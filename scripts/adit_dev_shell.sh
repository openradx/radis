#!/usr/bin/env bash

source "$(dirname "$0")/common.sh"

eval $COMPOSE_COMMAND_DEV exec web python manage.py shell_plus
