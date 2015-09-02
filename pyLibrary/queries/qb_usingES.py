# encoding: utf-8
#
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http:# mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
from collections import Mapping
from copy import deepcopy, copy

from pyLibrary import convert
from pyLibrary.env import elasticsearch, http
from pyLibrary.meta import use_settings
from pyLibrary.queries import qb, expressions, containers
from pyLibrary.queries.containers import Container
from pyLibrary.queries.containers.lists import ListContainer
from pyLibrary.queries.domains import is_keyword, SimpleSetDomain, NumericDomain, UniqueDomain
from pyLibrary.queries.es09 import setop as es09_setop
from pyLibrary.queries.es09.util import parse_columns, INDEX_CACHE, Column
from pyLibrary.queries.es14.aggs import es_aggsop, is_aggsop
from pyLibrary.queries.es14.deep import is_deepop, es_deepop
from pyLibrary.queries.es14.setop import is_setop, es_setop
from pyLibrary.queries.dimensions import Dimension
from pyLibrary.queries.es14.util import aggregates1_4
from pyLibrary.queries.namespace.typed import Typed
from pyLibrary.queries.query import Query, _normalize_where, _normalize_domain
from pyLibrary.debugs.logs import Log, Except
from pyLibrary.dot.dicts import Dict
from pyLibrary.dot import coalesce, split_field, set_default, literal_field, unwraplist, join_field, Null
from pyLibrary.dot.lists import DictList
from pyLibrary.dot import wrap, listwrap


class FromES(Container):
    """
    SEND GENERAL qb QUERIES TO ElasticSearch
    """

    def __new__(cls, *args, **kwargs):
        if (len(args) == 1 and args[0].get("index") == "meta") or kwargs.get("index") == "meta":
            output = FromESMetadata.__new__(FromESMetadata, *args, **kwargs)
            output.__init__(*args, **kwargs)
            return output
        else:
            return Container.__new__(cls)

    @use_settings
    def __init__(self, host, index, type=None, alias=None, name=None, port=9200, read_only=True, settings=None):
        Container.__init__(self, None, None)
        if not containers.config.default:
            containers.config.default.settings = settings
        self.settings = settings
        self.name = coalesce(name, alias, index)
        if read_only:
            self._es = elasticsearch.Alias(alias=coalesce(alias, index), settings=settings)
        else:
            self._es = elasticsearch.Cluster(settings=settings).get_index(read_only=read_only, settings=settings)
        self.settings.type = self._es.settings.type
        self.edges = Dict()
        self.worker = None
        self._columns = self.get_columns()
        # SWITCH ON TYPED MODE
        self.typed = any(c.name in ("$value", "$object") for c in self._columns)

    @staticmethod
    def wrap(es):
        output = FromES(es.settings)
        output._es = es
        return output

    def as_dict(self):
        settings = self.settings.copy()
        settings.settings = None
        return settings

    def __json__(self):
        return convert.value2json(self.as_dict())


    def __enter__(self):
        Log.error("No longer used")
        return self

    def __exit__(self, type, value, traceback):
        if not self.worker:
            return

        if isinstance(value, Exception):
            self.worker.stop()
            self.worker.join()
        else:
            self.worker.join()

    @property
    def query_path(self):
        return join_field(split_field(self.name)[1:])

    @property
    def url(self):
        return self._es.url

    def query(self, _query):
        try:
            query = Query(_query, schema=self)

            for n in self.namespaces:
                query = n.convert(query)
            if self.typed:
                query = Typed().convert(query)

            for s in listwrap(query.select):
                if not aggregates1_4.get(s.aggregate):
                    Log.error("ES can not aggregate " + s.name + " because '" + s.aggregate + "' is not a recognized aggregate")

            frum = query["from"]
            if isinstance(frum, Query):
                result = self.query(frum)
                q2 = query.copy()
                q2.frum = result
                return qb.run(q2)

            if is_deepop(self._es, query):
                return es_deepop(self._es, query)
            if is_aggsop(self._es, query):
                return es_aggsop(self._es, frum, query)
            if is_setop(self._es, query):
                return es_setop(self._es, query)
            if es09_setop.is_setop(query):
                return es09_setop.es_setop(self._es, None, query)

            Log.error("Can not handle")
        except Exception, e:
            e = Except.wrap(e)
            if "Data too large, data for" in e:
                http.post(self._es.cluster.path+"/_cache/clear")
                Log.error("Problem (Tried to clear Elasticsearch cache)", e)
            Log.error("problem", e)



    def get_relative_columns(self):
        if self._columns:
            return self._columns

        abs_columns=self._get_columns(self.settings.alias, self.path)


    def get_columns(self, _from_name=None):
        """
        _from_name - NOT MEANT FOR EXTERNAL USE
        """
        if _from_name is None:
            _from_name = self.name
        if not isinstance(_from_name, basestring):
            Log.error("Expecting string")

        output = INDEX_CACHE.get(_from_name)
        if output:
            # VERIFY es IS CONSISTENT
            if self.url != output.url:
                Log.error(
                    "Using {{name}} for two different containers\n\t{{existing}}\n\t{{new}}",
                    name=_from_name,
                    existing=output.url,
                    new=self._es.url
                )
            return output.columns

        path = split_field(_from_name)
        if len(path) > 1:
            # LOAD THE PARENT COLUMNS
            all_columns = self.get_columns(_from_name=path[0])
            this_name = join_field(path[1:])
            this = [c for c in all_columns if c.name == this_name][0]
            this_depth = len(this.nested_path)
            abs_columns = [
                c
                for c in all_columns
                if c.type != "nested"
            ]

            this_columns = []
            for c in abs_columns:
                #ABS FIELD NAMES
                c = copy(c)
                c.abs_name = c.name
                this_columns.append(c)

                #RELATIVE FIELD NAMES
                c = copy(c)
                c.relative = True
                if this_depth > len(c.nested_path):
                    c.name = ("."*(this_depth-len(c.nested_path)+1))+c.name
                else:
                    c.name = c.name[len(this.nested_path[0])+1:]
                this_columns.append(c)

            INDEX_CACHE[_from_name] = Dict(
                name=_from_name,
                url=self._es.url,
                columns=this_columns
            )
            return this_columns

        schema = self._es.get_schema()
        properties = schema.properties
        INDEX_CACHE[_from_name] = output = Dict()
        output.name = _from_name
        output.url = self._es.url
        output.columns = parse_columns(_from_name, None, properties)

        # CHECK CARDINALITY
        counts = self._es.get_cardinality(output.columns, join_field(split_field(_from_name)[1:]))

        for c in output.columns:
            col = counts[literal_field(c.name)]
            if not col:
                continue

            if c.type in ["long", "integer", "double", "float"]:
                if col.count <= 30:
                    c.domain = SimpleSetDomain(partitions=col.partitions)
                else:
                    c.domain = NumericDomain()
            elif c.type in ["boolean", "bool"]:
                c.domain.type = "boolean"
            else:
                if col.count <= 1000:
                    c.domain = SimpleSetDomain(partitions=col.partitions)
                else:
                    c.domain.type = UniqueDomain()
            c.domain = _normalize_domain(c.domain)

        return output.columns


    def get_column_names(self):
        # GET METADATA FOR INDEX
        # LIST ALL COLUMNS
        frum = self.get_columns()
        return frum.name

    def addDimension(self, dim):
        if isinstance(dim, list):
            Log.error("Expecting dimension to be a object, not a list:\n{{dim}}",  dim= dim)
        self._addDimension(dim, [])

    def _addDimension(self, dim, path):
        dim.full_name = dim.name
        for e in dim.edges:
            d = Dimension(e, dim, self)
            self.edges[d.full_name] = d

    def __getitem__(self, item):
        e = self.edges[item]
        return e

    def __getattr__(self, item):
        return self.edges[item]

    def normalize_edges(self, edges):
        output = DictList()
        for e in listwrap(edges):
            output.extend(self._normalize_edge(e))
        return output

    def _normalize_edge(self, edge):
        """
        RETURN A EDGE DEFINITION INTO A SIMPLE ARRAY OF PATH-LEAF
        DEFINITIONS [ {"name":<pathA>, "value":<pathB>}, ... ]

        USEFUL FOR DECLARING HIGH-LEVEL DIMENSIONS, AND RELIEVING LOW LEVEL PATH PAIRS
        """
        if isinstance(edge, basestring):
            e = self[edge]
            if e:
                domain = e.getDomain()
                fields = domain.dimension.fields

                if isinstance(fields, list):
                    if len(fields) == 1:
                        return [{"value": fields[0]}]
                    else:
                        return [{"name": (edge + "[" + str(i) + "]"), "value": v} for i, v in enumerate(fields)]
                elif isinstance(fields, Mapping):
                    return [{"name": (edge + "." + k), "value": v} for k, v in fields.items()]
                else:
                    Log.error("do not know how to handle")

            return [{
                        "name": edge,
                        "value": edge
                    }]
        else:
            return [{
                        "name": coalesce(edge.name, edge.value),
                        "value": edge.value
                    }]


    def update(self, command):
        """
        EXPECTING command == {"set":term, "where":where}
        THE set CLAUSE IS A DICT MAPPING NAMES TO VALUES
        THE where CLAUSE IS AN ES FILTER
        """
        command = wrap(command)
        schema = self._es.get_schema()

        # GET IDS OF DOCUMENTS
        results = self._es.search({
            "fields": listwrap(schema._routing.path),
            "query": {"filtered": {
                "query": {"match_all": {}},
                "filter": _normalize_where(command.where, self)
            }},
            "size": 200000
        })

        # SCRIPT IS SAME FOR ALL (CAN ONLY HANDLE ASSIGNMENT TO CONSTANT)
        scripts = DictList()
        for k, v in command.set.items():
            if not is_keyword(k):
                Log.error("Only support simple paths for now")
            if isinstance(v, Mapping) and v.doc:
                scripts.append({"doc": v.doc})
            else:
                scripts.append({"script": "ctx._source." + k + " = " + expressions.qb_expression_to_ruby(v)})

        if results.hits.hits:
            updates = []
            for h in results.hits.hits:
                for s in scripts:
                    updates.append({"update": {"_id": h._id, "_routing": unwraplist(h.fields[literal_field(schema._routing.path)])}})
                    updates.append(s)
            content = ("\n".join(convert.value2json(c) for c in updates) + "\n").encode('utf-8')
            response = self._es.cluster._post(
                self._es.path + "/_bulk",
                data=content,
                headers={"Content-Type": "application/json"}
            )
            if response.errors:
                Log.error("could not update: {{error}}", error=[e.error for i in response["items"] for e in i.values() if e.status not in (200, 201)])

class FromESMetadata(Container):
    """
    QUERY THE METADATA
    """

    @use_settings
    def __init__(self, host, index, alias=None, name=None, port=9200, settings=None):
        Container.__init__(self, None, schema=self)
        self.settings = settings
        self.name = coalesce(name, alias, index)
        self._es = elasticsearch.Cluster(settings=settings)
        self.metadata = self._es.get_metadata()
        self.columns = None

    @property
    def query_path(self):
        return None

    @property
    def url(self):
        return self._es.path + "/" + self.name.replace(".", "/")

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass

    def _get_columns(self):
        all_columns = []
        alias_done = set()
        metadata = self._es.get_metadata()
        for index, meta in qb.sort(metadata.indices.items(), {"value": 0, "sort": -1}):
            for _, properties in meta.mappings.items():
                columns = _parse_properties(index, properties.properties)
                for c in columns:
                    c.table = index
                    c.useSource = None

                all_columns.extend(columns)
                for a in meta.aliases:
                    # ONLY THE LATEST ALIAS IS CHOSEN TO GET COLUMNS
                    if a in alias_done:
                        continue
                    alias_done.add(a)
                    for c in columns:
                        cc = copy(c)
                        cc.table = a
                        all_columns.append(cc)

        self.columns = ListContainer([c.as_dict() for c in all_columns], {c.name: c for c in self.get_columns()})

    def query(self, _query):
        if not self.columns:
            self._get_columns()

        return self.columns.query(Query(set_default(
            {
                "from": self.columns,
                "sort": ["table", "name"]
            },
            _query.as_dict()
        )))

    def get_columns(self, _=None):
        """
        RETURN METADATA COLUMNS
        """
        if self.name == "meta.columns":
            return wrap([
                Column(
                    table="meta.columns",
                    name="table",
                    type="string",
                    nested_path=None,
                    useSource=True
                ),
                Column(
                    table="meta.columns",
                    name="name",
                    type="string",
                    nested_path=None,
                    useSource=True
                ),
                Column(
                    table="meta.columns",
                    name="type",
                    type="string",
                    nested_path=None,
                    useSource=True
                ),
                Column(
                    table="meta.columns",
                    name="nested_path",
                    type="string",
                    nested_path=None,
                    useSource=True
                )
            ])
        else:
            Log.error("Unknonw metadata: {{name}}.  Only `meta.columns` exists for now.",  name= self.settings.name)


def _parse_properties(index, properties):
    """
    ISOLATE THE DEALING WITH THE INDEX_CACHE,
    INDEX_CACHE IS REDUNDANT WHEN YOU HAVE metadata.columns
    """
    backup = INDEX_CACHE.get(index)
    INDEX_CACHE[index] = output = Dict()
    output.name = index
    columns = parse_columns(index, None, properties)
    INDEX_CACHE[index] = backup
    return columns
