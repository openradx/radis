# TODO

## High Priority

- Improve TokenFactory (+ ADIT)
- <https://docs.vespa.ai/en/operations/docker-containers.html#mounting-persistent-volumes>
- Change maxHits and maxOffset for farer pagination
  -- <https://docs.vespa.ai/en/reference/query-api-reference.html#native-execution-parameters>
  -- <https://pyvespa.readthedocs.io/en/latest/reference-api.html#queryprofile>
- Check if we can get rid of wsgi.py (also in RADIS)
  -- <https://forum.djangoproject.com/t/adding-asgi-support-to-runserver/2446/26>
  -- <https://github.com/django/django/pull/16634/files>
- Allow to configure reference names using a database model
  -- Reference: name (unique), match (unique)
- Sidebar like in <https://cord19.vespa.ai/search?query=pain> with filters: Age, Gender, Modality, Study Description
- Remove unneeded templatetags

## Fix

## Features

- Allow to re-feed Vespa documents
  -- Already WIP in branch vespa-re-feed
  -- Optionally allow to re-feed without full reset (only update documents, feed_iterable has an option for that)
- RAG app
  -- Let the user provide study date range (from, until), age, modality, keywords, question
  -- Pre-filter with database search (semantic search?)
  -- Add the search job to the queue
  -- Let a worker process this queue
  -- Give each filtered report to an LLM and let it answer the question
  -- LLM can be provided with GPU support (e.g. vLLM) or CPU only (e.g. llama-cpp-python)
  -- Add vLLM service with special "gpu" docker compose profile
  -- Collect all matching reports
  -- Notify user by Email when job is finished
  -- Let user browse the results
- Categories app
  -- LLMs answers to questions abouts reports to tag them with different categories like LAE, emphysema, ...
  -- Similar to RAG app (also maybe depends on it for accessing the LLM)
  -- Uses a catalog of questions
  -- When a new question is added to the catalog all existing and also upcoming reports will be evaluated
  -- Users can filter by those categories in the normal search (make this plug-in able)
- Subscriptions app
  -- Users can subscribe to Patient IDs, modalities, keywords, questions (see RAG app), categories (see Categories app)
  -- Cave, make sure categories app are evaluated before subscriptions
  -- Users get notified by Email when new matching reports arrive in the future
  -- Maybe link to report in RADIS in Email, optionally full report text in Email
  -- Maybe set a maximum number of reports in Email
- Allow to export collections to ADIT to transfer the corresponding studies

## Maybe

- Adjust the summary dynamic snippets of the search results
  -- <https://docs.vespa.ai/en/document-summaries.html>
  -- Unfortunately, ApplicationConfiguration does not allow to put the configuration inside the content cluster (see link above)
  --- <https://github.com/vespa-engine/pyvespa/blob/75c64ab144f98155387ff1f461632b889c19bd6e/vespa/package.py#L1490>
  --- <https://github.com/vespa-engine/pyvespa/blob/master/vespa/templates/services.xml>
  -- That's why we would need to manipulate the XML files ourselves (maybe with <https://docs.python.org/3/library/xml.etree.elementtree.html>)
  -- or simply wait for <https://github.com/vespa-engine/pyvespa/issues/520>
- Put an extra "indication" field into the schema
  -- Also must be included in the ranking expression, see <https://pyvespa.readthedocs.io/en/latest/getting-started-pyvespa.html#Define-ranking>
- Multi node Vespa example setup
  -- <https://github.com/vespa-engine/sample-apps/blob/master/examples/operations/multinode-HA/>
- Standalone logging server
  -- SigNoz <https://github.com/signoz/signoz>
  -- Loki <https://github.com/grafana/loki>
  -- ELK stack <https://github.com/deviantony/docker-elk>

## Transfer to RADIS

- Rename populate_dev_db to populate_db
- .env files in project dir (instead of compose dir)
- Correct help in populate_dev_db command
- Delete reset_dev_db and add reset option to populate_dev_db
- globals.d.ts
- PageSizeSelectMixin improvements
