# Project Atlas - dashboard image
# AT-26-L1-002 | NFR-02
#
# Two stages. The build stage needs Node, npm and the whole dependency tree;
# the result is a directory of static files that needs none of them. Shipping
# the builder would put a package manager and several hundred megabytes of
# node_modules into a container that only has to serve HTML — more to patch,
# more to attack, for no benefit.

# ----------------------------------------------------------------- build
FROM node:22-alpine AS build

WORKDIR /build

# Dependencies are copied and installed before the source. Docker caches layers,
# and the source changes far more often than the dependency list, so this makes
# the common rebuild skip the install entirely.
COPY package.json package-lock.json ./

# `npm ci` rather than `npm install`: it installs exactly what the lock file
# records and fails if package.json and the lock file disagree. `npm install`
# would quietly resolve new versions, which defeats the point of committing a
# lock file at all.
RUN npm ci

COPY . .

# The API base is baked in at build time. Vite substitutes import.meta.env at
# compile time - there is no runtime environment in a static bundle - so this
# is a build argument and not a container environment variable.
ARG VITE_API_BASE=http://localhost:8000
ENV VITE_API_BASE=$VITE_API_BASE

RUN npm run build

# ----------------------------------------------------------------- serve
# The unprivileged nginx image runs as uid 101 and listens on 8080. The
# standard image starts as root to bind port 80 and then drops privileges;
# this one never holds root at all, which matches the arrangement every other
# service in this stack already uses.
FROM nginxinc/nginx-unprivileged:alpine

COPY --from=build /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
