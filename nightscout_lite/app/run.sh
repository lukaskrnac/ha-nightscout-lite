#!/bin/sh
# s6-overlay hands the program a cleaned environment; with-contenv puts the container's
# variables (SUPERVISOR_TOKEN among them) back. The bridge also reads them from s6's files
# itself, so a base image without with-contenv still works.
for wrapper in /usr/bin/with-contenv /command/with-contenv; do
    if [ -x "$wrapper" ]; then
        exec "$wrapper" python3 -u /app/main.py
    fi
done
exec python3 -u /app/main.py
