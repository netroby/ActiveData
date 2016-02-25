# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import flask
from flask import Response

from active_data import record_request
from active_data.actions.static import BLANK
from pyLibrary import convert, strings
from pyLibrary.debugs.exceptions import Except
from pyLibrary.debugs.logs import Log
from pyLibrary.debugs.profiles import CProfiler
from pyLibrary.dot import coalesce
from pyLibrary.maths import Math
from pyLibrary.queries import jx, meta
from pyLibrary.queries.containers import Container
from pyLibrary.queries.meta import TOO_OLD
from pyLibrary.strings import expand_template
from pyLibrary.thread.threads import Thread
from pyLibrary.times.dates import Date
from pyLibrary.times.durations import MINUTE
from pyLibrary.times.timer import Timer

from active_data.actions import save_query


def query(path):
    with CProfiler():
        query_timer = Timer("total duration")
        body = flask.request.data
        try:
            with query_timer:
                if not body.strip():
                    return Response(
                        convert.unicode2utf8(BLANK),
                        status=400,
                        headers={
                            "access-control-allow-origin": "*",
                            "content-type": "text/html"
                        }
                    )

                text = convert.utf82unicode(body)
                text = replace_vars(text, flask.request.args)
                data = convert.json2value(text)
                record_request(flask.request, data, None, None)
                if data.meta.testing:
                    _test_mode_wait(data)

                result = jx.run(data)

                if isinstance(result, Container):  #TODO: REMOVE THIS CHECK, jx SHOULD ALWAYS RETURN Containers
                    result = result.format(data.format)

                if data.meta.save:
                    result.meta.saved_as = save_query.query_finder.save(data)

                result.meta.timing.total = "{{TOTAL_TIME}}"  # TIMING PLACEHOLDER

                json_timer = Timer("jsonification")
                with json_timer:
                    response_data = convert.unicode2utf8(convert.value2json(result))

            with Timer("post timer"):
                # IMPORTANT: WE WANT TO TIME OF THE JSON SERIALIZATION, AND HAVE IT IN THE JSON ITSELF.
                # WE CHEAT BY DOING A (HOPEFULLY FAST) STRING REPLACEMENT AT THE VERY END
                timing_replacement = b'"total": ' + str(Math.round(query_timer.duration.seconds, digits=8)) +\
                                     ', "jsonification": ' + str(Math.round(json_timer.duration.seconds, digits=8))
                response_data = response_data.replace(b'"total": "{{TOTAL_TIME}}"', timing_replacement)
                Log.note("Response is {{num}} bytes in {{duration}}", num=len(response_data), duration=query_timer.duration)

                return Response(
                    response_data,
                    status=200,
                    headers={
                        "access-control-allow-origin": "*",
                        "content-type": result.meta.content_type
                    }
                )
        except Exception, e:
            e = Except.wrap(e)
            return _send_error(query_timer, body, e)


def _test_mode_wait(query):
    """
    WAIT FOR METADATA TO ARRIVE ON INDEX
    :param query: dict() OF REQUEST BODY
    :return: nothing
    """

    m = meta.singlton
    now = Date.now()
    end_time = now + MINUTE

    # MARK COLUMNS DIRTY
    with m.columns.locker:
        m.columns.update({
            "clear": [
                "partitions",
                "count",
                "cardinality",
                "last_updated"
            ],
            "where": {"eq": {"table": query["from"]}}
        })

    # BE SURE THEY ARE ON THE todo QUEUE FOR RE-EVALUATION
    cols = [c for c in m.get_columns(table=query["from"]) if c.type not in ["nested", "object"]]
    for c in cols:
        Log.note("Mark {{column}} dirty at {{time}}", column=c.name, time=now)
        c.last_updated = now - TOO_OLD
        m.todo.push(c)

    while end_time > now:
        # GET FRESH VERSIONS
        cols = [c for c in m.get_columns(table=query["from"]) if c.type not in ["nested", "object"]]
        for c in cols:
            if not c.last_updated or c.cardinality == None :
                Log.note(
                    "wait for column (table={{col.table}}, name={{col.name}}) metadata to arrive",
                    col=c
                )
                break
        else:
            break
        Thread.sleep(seconds=1)
    for c in cols:
        Log.note(
            "fresh column name={{column.name}} updated={{column.last_updated|date}} parts={{column.partitions}}",
            column=c
        )


def _send_error(active_data_timer, body, e):
    record_request(flask.request, None, body, e)
    Log.warning("Could not process\n{{body}}", body=body.decode("latin1"), cause=e)
    e = e.as_dict()
    e.meta.timing.total = active_data_timer.duration.seconds
    return Response(
        convert.unicode2utf8(convert.value2json(e)),
        status=400,
        headers={
            "access-control-allow-origin": "*",
            "content-type": "application/json"
        }
    )


def replace_vars(text, params=None):
    """
    REPLACE {{vars}} WITH ENVIRONMENTAL VALUES
    """
    start = 0
    var = strings.between(text, "{{", "}}", start)
    while var:
        replace = "{{" + var + "}}"
        index = text.find(replace, 0)
        end = index + len(replace)

        try:
            replacement = unicode(Date(var).unix)
            text = text[:index] + replacement + text[end:]
            start = index + len(replacement)
        except Exception, _:
            start += 1

        var = strings.between(text, "{{", "}}", start)

    text = expand_template(text, coalesce(params, {}))
    return text