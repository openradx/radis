# RADIS

## About

RADIS (Radiology Report Archive and Discovery System) is an application to archive, query and collect radiology reports.

## Installation

This repository is built to run directly in Codespaces. To run it locally a few preparations have to be taken care of:

### Basic setup:
0. Install docker. See https://docs.docker.com/.
1. Setup a virtal environment with the latest python version (>= 3.11). 
2. Install poetry. See https://python-poetry.org/docs/.
3. Setup the radis poetry project environment with `poetry install`
4. Copy (or rename) `example.env` in the root of this repo to `.env` and `.env.dev`. Edit them if needed.

### Proxy setup:
5. If you are behind a proxy server, enter proxy details at ~/.docker/config.json or in the .env file(s).
6. Depending on your proxy settings it might be necessary to unset the proxy environment variables in the radis web container to enable local communication with the vespa database via vespa-cli. This can be achieved by editing the command section of the web service in `docker-compose.dev.yml` (or `docker-compose.prod.yml`) inside `compose/`. Insert `http_proxy= && HTTP_PROXY=` at some point before database communication is established.

## Features

## Upcoming features

## Screenshots

## Architectural overview

## Contributors

[![medihack](https://github.com/medihack.png?size=50)](https://github.com/medihack)

## Disclaimer

RADIS is not a certified medical product. So use at your own risk.

## License

- GPLv3
