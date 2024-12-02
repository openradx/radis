# TODO

## High Priority

- Use Alpine directive instead data for age range slider
- Rename all derived models from AppSettings (to be congruent with ADIT)
- Check if for RAG ranking should be turned off for performance improvements (and using some fixed sort order)
- Some present provider.max_results to the user somehow, especially important if the query results (step 1) is larger
- task control panel
- Upgrade Github actions workflows (also ADIT)
- Check if we can get rid of wsgi.py (also in RADIS)
  - <https://forum.djangoproject.com/t/adding-asgi-support-to-runserver/2446/26>
  - <https://github.com/django/django/pull/16634/files>
- Allow to configure reference names using a database model
  - Reference: name (unique), match (unique)
- Remove unneeded templatetags
- Are pandas and openpyxl needed as deps?!

## Fix

- Update list of stop words
  - Postgresql has stuff like "kein", "keine" in its stop words list
  - Customize the list of stop words
  - The original stop word list can be found in the container at /usr/share/postgresql/16/tsearch_data
  - There are multiple ways to solve that:
    - Completely disable stop words: <https://stackoverflow.com/a/2227235/166229>
    - Create complete new dictionaries
    - Mount manipulated stop word files (<https://github.com/postgres/postgres/tree/master/src/backend/snowball/stopwords>)

## Features

- RAG app
  - Let the user provide study date range (from, until), age, modality, keywords, question
  - Add the search job to the queue (similar to transfer jobs in ADIT)
  - Let a worker process the queue in its own service
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
- Fetch favicon for pacs_link
- Categories app
  - LLMs answers to questions abouts reports to tag them with different categories like LAE, emphysema, ...
  - Similar to RAG app (also maybe depends on it for accessing the LLM)
  - Uses a catalog of questions
  - When a new question is added to the catalog all existing and also upcoming reports will be evaluated
  - Users can filter by those categories in the normal search (make this plug-in able)
- Subscriptions app
  - Users can subscribe to Patient IDs, questions (see RAG app), categories (see Categories app)
  - Can also filter by modalities, study description, patient sex, patient age range
  - Cave, make sure categories app are evaluated before subscriptions
  - Users get notified by Email when new matching reports arrive in the future
  - Maybe link to report in RADIS in Email, optionally full report text in Email
  - Maybe only allow a maximum number of hits
  - Maybe set a maximum number of reports in Email
- Allow to export collections to ADIT to transfer the corresponding studies

## Maybe

- Look into distanceThreshold when using semantic/hybrid search
  -- In normal nearestNeighbor search every document is included as all documents are neighbors
  -- That is why we currently only use semantic stuff as ranking algorithm
  -- We could add a distanceThreshold to only allow really near neighbors
  -- But the threshold is unclear (not sure if we should allow to the user to specify)
- Rename reports model fields to something in the HL7 FHIR standard
  - Interesting resources in this regard are:
    - <https://hl7.org/fhir/patient.html>
    - <https://hl7.org/fhir/observation.html>
    - <https://hl7.org/fhir/diagnosticreport.html>
    - <https://hl7.org/fhir/imagingstudy.html>
- Standalone logging server
  - SigNoz <https://github.com/signoz/signoz>
  - Loki <https://github.com/grafana/loki>
  - ELK stack <https://github.com/deviantony/docker-elk>

## Transfer to ADIT

- Prepare Django translations
- Replace me-3 in control_panel.html with gap-3 of surrounding div
- .env files in project dir (instead of compose dir)
- Correct help in populate_dev_db command
- globals.d.ts
- rename all Alpine components to Uppercase
- Add metaclass=ABCMeta to abstract core/models and core/views (also core/tables and core/filters even in RADIS)
