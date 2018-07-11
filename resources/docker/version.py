import os

from mo_dots import coalesce
from mo_files import File
from mo_future import text_type
from mo_json import value2json
from pyLibrary.env.git import get_git_revision

print("Uses *.dockerfile environment variables to write a verion.json file")

File("version.json").write(value2json(
    {
        "source": os.environ.get('REPO_URL'),
        "version": coalesce(os.environ.get('REPO_TAG'), os.environ.get('REPO_BRANCH')),
        "commit": get_git_revision(),
        "build": os.environ.get('BUILD_URL')
    },
    pretty=True
) + text_type("\n"))