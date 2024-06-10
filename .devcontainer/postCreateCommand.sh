#!/usr/bin/env bash

poetry install
poetry run invoke init-workspace
poetry run invoke download-llm -m tinyllama-1b-q2
