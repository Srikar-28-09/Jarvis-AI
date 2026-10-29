# Host Bridge

The Host Bridge is an optional local service for computer-control actions on the machine running it. It is separate from the Odysseus web process and listens on `127.0.0.1:8096` by default. Keep the generated authentication token private; the bridge creates it in the configured Jarvis workspace when it starts.

## Linux with systemd

The included user service expects the project environment at `.venv`. From the repository root, create it and install the Python dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
deactivate
```

Then install and start the user service:

```sh
./install-host-bridge-user-service.sh
```

The script installs `odysseus-host-bridge.service` under the current user's systemd configuration and starts it. The service restarts if it exits. To inspect or stop it:

```sh
systemctl --user status odysseus-host-bridge.service
systemctl --user stop odysseus-host-bridge.service
```

## Run manually

Activate an environment with the project dependencies installed, then run:

```sh
python run_host_bridge.py
```

On Windows, run this command from PowerShell with the project's Python environment activated. The bridge uses its Windows host actions and hotkey implementation there.

The Docker Compose setup mounts `JARVIS_WORKSPACE_DIR` at `/workspace/jarvis`; the shared `.host_bridge_token` file is ignored by Git. Set `JARVIS_WORKSPACE_DIR` in `.env` if the workspace is in a different location. The bridge binds locally by default and discovers Docker gateway interfaces on Linux for requests from the container.
