#!/bin/sh
P="podman --root /var/mnt/data/overleaf-ce-test/storage --runroot /run/user/1000/ol-ce-runroot"
$P network create olnet 2>/dev/null
$P run -d --name mongo --network olnet -v ol-mongo:/data/db docker.io/library/mongo:8.0 --replSet overleaf --bind_ip_all
$P run -d --name redis --network olnet -v ol-redis:/data docker.io/library/redis:6.2
sleep 8
$P exec mongo mongosh --quiet --eval "rs.initiate({_id:\"overleaf\",members:[{_id:0,host:\"mongo:27017\"}]})"
$P run -d --name sharelatex --network olnet -p 127.0.0.1:8080:80 -v ol-data:/var/lib/overleaf \
  -e OVERLEAF_INVITE_TOKEN_SECRET=$(openssl rand -hex 24) -e OVERLEAF_APP_NAME=test -e OVERLEAF_MONGO_URL=mongodb://mongo:27017/sharelatex \
  -e OVERLEAF_REDIS_HOST=redis -e REDIS_HOST=redis -e ENABLED_LINKED_FILE_TYPES=project_file,project_output_file \
  -e ENABLE_CONVERSIONS=true -e EMAIL_CONFIRMATION_DISABLED=true -e OVERLEAF_SITE_URL=http://localhost:8080 \
  docker.io/sharelatex/sharelatex:latest
