# Backups

Database backups are made by the django-dbbackup app. A periodic task runs the `dbbackup` management command every night at 3 am (`BACKUP_CRON`; set `BACKUP_ENABLED=false` to turn it off). Backups are written to the host directory in `BACKUP_DIR`, which is mounted into the containers as `/backups`, and the last 30 are kept.

Run a backup by hand with `uv run cli db-backup`, and restore the latest one with `uv run cli db-restore`. Both need a running `web` container.
