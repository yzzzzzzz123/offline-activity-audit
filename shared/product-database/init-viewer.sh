#!/bin/bash
# Runs during initialization of a new data volume, whether sourced or executable.
viewer_password="$(cat /run/secrets/viewer_password)"
if [[ ! "$viewer_password" =~ ^[A-Za-z0-9_-]{24,128}$ ]]; then
    echo 'Viewer password must contain 24-128 ASCII letters, digits, underscores or hyphens.' >&2
    exit 1
fi
MYSQL_PWD="$(cat /run/secrets/root_password)" mysql --no-defaults --protocol=socket --user=root <<SQL
CREATE USER 'product_viewer'@'%' IDENTIFIED BY '${viewer_password}' REQUIRE SSL;
GRANT SELECT, SHOW VIEW ON product_catalog.* TO 'product_viewer'@'%';
SQL
unset viewer_password
