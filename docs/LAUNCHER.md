# Project Atlas Launcher

The launcher is a convenience wrapper around the existing web platform. It does not create a second application and it does not duplicate the API, database, broker, or Dashboard. It starts the same Docker Compose project and opens the same Dashboard URL in an application-style browser window.

## Install on Kali

From the project root:

```bash
chmod +x tools/atlas_start.sh tools/atlas_stop.sh tools/install_atlas_launcher.sh
tools/install_atlas_launcher.sh
```

The launcher is installed as `Project Atlas` in the desktop application menu. It uses the project directory from which the installer was run, so it also works when the repository is not under `/home/unknown/atlas`.

## Start

Click **Project Atlas** in the application menu, or run:

```bash
tools/atlas_start.sh
```

The script runs `docker compose up -d`, waits for `/health`, and then opens `http://127.0.0.1:5173`. It prefers Chromium application mode and falls back to Firefox or `xdg-open` if Chromium is unavailable.

## Stop

Use:

```bash
tools/atlas_stop.sh
```

This runs `docker compose stop`. It deliberately does not run `docker compose down -v`, so database data, MQTT state, certificates, and Dynamic Security volumes are preserved.

## Web access remains available

The regular website is unchanged. It remains available at `http://127.0.0.1:5173` in any browser. The launcher simply opens that same site in a separate window. Updates to the React Dashboard, API, broker, and database therefore apply to both access methods.
