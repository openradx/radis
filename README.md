# RADIS

## About

RADIS (Radiology Report Archive and Discovery System) is a web application to archive, query and collect radiology reports.

## Features

- Store radiology reports in structured way and allow to retrieve them
- Full text search using different algorithms (BM25, vector hybrid search)
- Search and filter reports using Retrieval Augmented Generation (RAG)
- Add user specific notes to reports
- Add reports to creatable collections
- Directly open the PACS viewer with the corresponding study

## Upcoming features

- Subscriptions to get informed by Email when newly added reports match some specific criteria
- Automatically categorize all reports by using an LLM
- Export collected reports and allow the corresponding studies to be transferred by using [ADIT](https://github.com/openradx/adit)

## API Client

[RADIS Client](https://github.com/openradx/radis-client) is a Python library to search for reports on RADIS in a programmatic way. It also allows admins to feed new reports to RADIS.

## Screenshots

Upcoming ...

## Architectural overview

RADIS is built using the Django web framework, and data is stored in a [PostgreSQL](https://www.postgresql.org/) database, which is also use for full text search. The design of RADIS is very modular so that other text search databases can easily be integrated.

## Contributors

[![medihack](https://github.com/medihack.png?size=50)](https://github.com/medihack)
[![hagisgit](https://github.com/hagisgit.png?size=50)](https://github.com/hagisgit)
[![julihereu](https://github.com/julihereu.png?size=50)](https://github.com/julihereu)

## Disclaimer

RADIS is not a certified medical product. So use at your own risk.

## License

- AGPLv3
