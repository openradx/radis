# TODO

## High Priority

- task control panel
- Limit RAG search result choice (stop when there x positive results)
- Limit RAG search by maximum number of reports that gets processed
- Show metadata in report details
- Age slider
  - <https://tailwindcomponents.com/component/multi-range-slider>
  - <https://codepen.io/glitchworker/pen/XVdKqj>
  - <https://medium.com/@predragdavidovic10/native-dual-range-slider-html-css-javascript-91e778134816>
- Upgrade Github actions workflows (also ADIT)
- Allow to set filters when using search
  - from:2024-01-01 until:2024-02-01 modality:ct description:"CT\*"
- Improve TokenFactory (+ ADIT)
- <https://docs.vespa.ai/en/operations/docker-containers.html#mounting-persistent-volumes>
- Change maxHits and maxOffset for farer pagination
  - <https://docs.vespa.ai/en/reference/query-api-reference.html#native-execution-parameters>
  - <https://pyvespa.readthedocs.io/en/latest/reference-api.html#queryprofile>
- Check if we can get rid of wsgi.py (also in RADIS)
  - <https://forum.djangoproject.com/t/adding-asgi-support-to-runserver/2446/26>
  - <https://github.com/django/django/pull/16634/files>
- Allow to configure reference names using a database model
  - Reference: name (unique), match (unique)
- Sidebar like in <https://cord19.vespa.ai/search?query=pain> with filters: Age, Gender, Modality, Study Description
- Remove unneeded templatetags
- Are pandas and openpyxl needed as deps?!
- Remove Redis if not needed anymore

## Fix

## Features

- Allow to re-feed Vespa documents
  - Already WIP in branch vespa-re-feed
  - Optionally allow to re-feed without full reset (only update documents, feed_iterable has an option for that)
- RAG app
  - Let the user provide study date range (from, until), age, modality, keywords, question
  - Add the search job to the queue (similar to transfer jobs in ADIT)
  - Let a worker process the queue in its own service
  - Pre-filter reports with a Vespa database search (semantic search?)
  - Give each filtered report to an LLM and let it answer the question
  - Constraint the output of the LLM, multiple possibilities for that:
    - <https://github.com/outlines-dev/outlines>
    - <https://github.com/guidance-ai/guidance>
    - <https://github.com/ggerganov/llama.cpp/blob/master/grammars/README.md>
    - <https://lmql.ai/>
    - <https://github.com/stanfordnlp/dspy>
  - LLM can be provided with GPU support or CPU only (both supported by llama.cpp)
    - <https://docs.docker.com/compose/gpu-support/>
  - Collect all matching reports
  - Notify user by Email when job is finished
  - Let user browse the results
- Fetch favicon for links
- Categories app
  - LLMs answers to questions abouts reports to tag them with different categories like LAE, emphysema, ...
  - Similar to RAG app (also maybe depends on it for accessing the LLM)
  - Uses a catalog of questions
  - When a new question is added to the catalog all existing and also upcoming reports will be evaluated
  - Users can filter by those categories in the normal search (make this plug-in able)
- Subscriptions app
  - Users can subscribe to Patient IDs, modalities, keywords, questions (see RAG app), categories (see Categories app)
  - Cave, make sure categories app are evaluated before subscriptions
  - Users get notified by Email when new matching reports arrive in the future
  - Maybe link to report in RADIS in Email, optionally full report text in Email
  - Maybe set a maximum number of reports in Email
- Allow to export collections to ADIT to transfer the corresponding studies

## Maybe

- Look into distanceThreshold when using semantic/hybrid search
  -- In normal nearestNeighbor search every document is included as all documents are neighbors
  -- That is why we currently only use semantic stuff as ranking algorithm
  -- We could add a distanceThreshold to only allow really near neighbors
  -- But the threshold is unclear (not sure if we should allow to the user to specify)
  -- <https://docs.vespa.ai/en/nearest-neighbor-search-guide.html#strict-filters-and-distant-neighbors>
- Rename reports model fields to something in the HL7 FHIR standard
  - Interesting resources in this regard are:
    - <https://hl7.org/fhir/patient.html>
    - <https://hl7.org/fhir/observation.html>
    - <https://hl7.org/fhir/diagnosticreport.html>
    - <https://hl7.org/fhir/imagingstudy.html>
- Adjust the summary dynamic snippets of the search results
  - <https://docs.vespa.ai/en/document-summaries.html>
  - Unfortunately, ApplicationConfiguration does not allow to put the configuration inside the content cluster (see link above)
    - <https://github.com/vespa-engine/pyvespa/blob/75c64ab144f98155387ff1f461632b889c19bd6e/vespa/package.py#L1490>
    - <https://github.com/vespa-engine/pyvespa/blob/master/vespa/templates/services.xml>
  - That's why we would need to manipulate the XML files ourselves (maybe with <https://docs.python.org/3/library/xml.etree.elementtree.html>)
    - or simply wait for <https://github.com/vespa-engine/pyvespa/issues/520>
- Put an extra "indication" field into the schema
  - Also must be included in the ranking expression, see <https://pyvespa.readthedocs.io/en/latest/getting-started-pyvespa.html#Define-ranking>
- Multi node Vespa example setup
  - <https://github.com/vespa-engine/sample-apps/blob/master/examples/operations/multinode-HA/>
- Standalone logging server
  - SigNoz <https://github.com/signoz/signoz>
  - Loki <https://github.com/grafana/loki>
  - ELK stack <https://github.com/deviantony/docker-elk>

## Transfer to RADIS

- Replace me-3 in control_panel.html with gap-3 of surrounding div
- Rename populate_dev_db to populate_db
- .env files in project dir (instead of compose dir)
- Correct help in populate_dev_db command
- Delete reset_dev_db and add reset option to populate_dev_db
- globals.d.ts
- rename all Alpine components to Uppercase
- Turn off debug logging in Celery
- Add metaclass=ABCMeta to abstract core/models and core/views (also core/tables and core/filters even in RADIS)
