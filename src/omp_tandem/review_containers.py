"""Local Docker process boundary for explicitly authorized review checks."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import docker
import httpx
from docker.errors import DockerException, NotFound
from docker.types import LogConfig, Mount

from .review_check_state import valid_container

API_TIMEOUT = 3


def _client(policy):
    # Never discover an endpoint, proxy, registry or image implicitly at execution.
    client = docker.APIClient(
        base_url="unix://" + policy["socket"],
        version=policy["api_version"],
        timeout=API_TIMEOUT,
    )
    client.trust_env = False
    return client


def _image(client, identifier):
    image = client.inspect_image(identifier)
    if image.get("Os") != "linux" or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image.get("Id", "")
    ):
        raise ValueError("Review checks require an immutable Linux image")
    if (image.get("Config") or {}).get("Volumes"):
        raise ValueError("Review images must not declare additional volumes")
    return image


def _network(client, name):
    if name == "none":
        return name
    network = client.inspect_network(name)
    if network.get("Driver") != "bridge" or not re.fullmatch(
        r"[0-9a-f]{64}", network.get("Id", "")
    ):
        raise ValueError("Review checks require none or an existing bridge network")
    return network["Id"]


def resolve_container(image, *, context=None, network="none"):
    """Observe local identities without pulling an image or creating resources."""
    if not isinstance(image, str) or not image.strip():
        raise ValueError("--allow-review-checks requires --review-check-image")
    if context is not None and not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]*", context
    ):
        raise ValueError("Invalid Docker context name")
    try:
        command = ["docker", "context", "inspect"]
        if context:
            command.append(context)
        command.extend(["--format", "{{json .Endpoints.docker.Host}}"])
        observed = subprocess.run(command, capture_output=True, check=True, timeout=10)
        endpoint = urlsplit(json.loads(observed.stdout))
        if (
            endpoint.scheme != "unix"
            or endpoint.netloc
            or endpoint.query
            or endpoint.fragment
            or not endpoint.path.startswith("/")
        ):
            raise ValueError("Review checks require a local Docker Unix socket")
        socket = Path(unquote(endpoint.path)).resolve(strict=True)
        if not stat.S_ISSOCK(socket.stat().st_mode):
            raise ValueError("Docker endpoint is not a Unix socket")
        # Negotiate before constructing the SDK client with an explicit version.
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket), retries=0),
            base_url="http://docker",
            trust_env=False,
            timeout=API_TIMEOUT,
        ) as probe:
            response = probe.get("/version")
            response.raise_for_status()
            version = response.json().get("ApiVersion", "")
        if (
            not re.fullmatch(r"1\.[0-9]{1,3}", version)
            or int(version.split(".")[1]) < 41
        ):
            raise ValueError("Docker Engine API 1.41 or newer is required")
        policy = {"socket": str(socket), "api_version": version}
        with _client(policy) as client:
            info = client.info()
            if info.get("OSType") != "linux":
                raise ValueError("Review checks require a Linux Docker engine")
            policy.update(
                executor="docker",
                daemon_id=info.get("ID"),
                image_id=_image(client, image)["Id"],
                network_mode=_network(client, network),
                platform="linux",
            )
        if not valid_container(policy):
            raise ValueError("Docker returned an invalid resolved review identity")
        return policy
    except (
        DockerException,
        httpx.HTTPError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        raise ValueError(
            "Cannot pin local Docker review execution; check the context and prepare "
            f"the selected image/network explicitly ({type(error).__name__})"
        ) from error


class ReviewContainer:
    """One non-replaying create/start and cleanup of only its labelled resource."""

    def __init__(self, policy, *, scope_id, attempt_id, run_id, workspace, command):
        if not valid_container(policy):
            raise ValueError("Review container authorization is invalid")
        self.policy = policy
        self.client = _client(policy)
        self.name = "omp-tandem-check-" + run_id
        self.labels = {
            "org.omp-tandem.scope": scope_id,
            "org.omp-tandem.attempt": attempt_id,
            "org.omp-tandem.run": run_id,
        }
        self.workspace = str(Path(workspace).resolve(strict=True))
        self.command = command
        self.user = f"{os.getuid()}:{os.getgid()}"
        self.identifier = None
        self.create_requested = False

    def _daemon(self):
        info = self.client.info()
        if info.get("ID") != self.policy["daemon_id"] or info.get("OSType") != "linux":
            raise ValueError("The authorized Docker daemon identity changed")

    def _owned(self, container):
        identifier = container.get("Id", "")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", identifier)
            or (self.identifier is not None and identifier != self.identifier)
            or any(
                (container.get("Config", {}).get("Labels") or {}).get(key) != value
                for key, value in self.labels.items()
            )
        ):
            raise ValueError("Review container ownership is not confirmed")
        return container

    def create(self, environment):
        self._daemon()
        image = _image(self.client, self.policy["image_id"])
        if (
            image["Id"] != self.policy["image_id"]
            or _network(self.client, self.policy["network_mode"])
            != self.policy["network_mode"]
        ):
            raise ValueError("The authorized image or network identity changed")
        # Docker merges image defaults. Override nonselected defaults with empty
        # values so image ENV cannot inject credentials or configuration implicitly.
        defaults = dict(
            item.split("=", 1)
            for item in (image.get("Config") or {}).get("Env") or []
            if "=" in item
        )
        values = {name: "" for name in defaults}
        values["PATH"] = defaults.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        values.update(environment)
        host = self.client.create_host_config(
            mounts=[
                Mount("/workspace", self.workspace, "bind", propagation="rprivate")
            ],
            network_mode=self.policy["network_mode"],
            read_only=True,
            init=True,
            privileged=False,
            ipc_mode="private",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            tmpfs={"/tmp": "rw,nosuid,nodev,mode=1777"},
            pids_limit=512,
            log_config=LogConfig(type="none"),
        )
        self.create_requested = True
        created = self.client.create_container(
            image=self.policy["image_id"],
            entrypoint=["/bin/sh"],
            command=["-c", self.command],
            working_dir="/workspace",
            user=self.user,
            environment=values,
            labels=self.labels,
            name=self.name,
            host_config=host,
            healthcheck={"Test": ["NONE"]},
            stdin_open=False,
            use_config_proxy=False,
            tty=False,
        )
        identifier = created.get("Id", "")
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError("Docker did not acknowledge an immutable container ID")
        self.identifier = identifier
        observed = self.inspect()
        config, host = observed["Config"], observed["HostConfig"]
        mounts = host.get("Mounts") or []
        if (
            observed.get("Image") != self.policy["image_id"]
            or config.get("WorkingDir") != "/workspace"
            or config.get("User") != self.user
            or config.get("Entrypoint") != ["/bin/sh"]
            or config.get("Cmd") != ["-c", self.command]
            or config.get("Volumes")
            or config.get("Tty")
            or config.get("Healthcheck", {}).get("Test") != ["NONE"]
            or not host.get("Init")
            or not host.get("ReadonlyRootfs")
            or host.get("Privileged")
            or host.get("PidMode") not in {None, "", "private"}
            or host.get("IpcMode") != "private"
            or host.get("NetworkMode") != self.policy["network_mode"]
            or "ALL" not in (host.get("CapDrop") or [])
            or not {"no-new-privileges", "no-new-privileges=true"}.intersection(
                host.get("SecurityOpt") or []
            )
            or host.get("Binds")
            or host.get("Devices")
            or len(mounts) != 1
            or mounts[0].get("Type") != "bind"
            or mounts[0].get("Source") != self.workspace
            or mounts[0].get("Target") != "/workspace"
            or mounts[0].get("ReadOnly")
            or set(host.get("Tmpfs") or {}) != {"/tmp"}
            or host.get("LogConfig", {}).get("Type") != "none"
            or observed.get("State", {}).get("Running")
        ):
            raise ValueError(
                "Docker did not preserve the review isolation configuration"
            )

    def inspect(self):
        return self._owned(self.client.inspect_container(self.identifier))

    def attach(self):
        return self.client.attach_socket(
            self.identifier,
            params={"stdout": True, "stderr": True, "stream": True, "logs": False},
        )

    def start(self):
        self.client.start(self.identifier)

    def remove(self):
        if not self.create_requested:
            return True
        self._daemon()
        try:
            observed = self._owned(
                self.client.inspect_container(self.identifier or self.name)
            )
        except NotFound:
            # An unacknowledged create can still arrive later. Absence is not
            # proof until the resource's immutable identity has been observed.
            return self.identifier is not None
        self.identifier = observed["Id"]
        try:
            self.client.remove_container(self.identifier, force=True, v=True)
        except NotFound:
            pass
        except (DockerException, OSError):
            # Removal acknowledgement may be lost; observation is safe, unlike
            # replaying create/start. The following read must prove absence.
            pass
        try:
            self.client.inspect_container(self.identifier)
        except NotFound:
            return True
        return False

    def close(self, stream=None):
        try:
            if stream is not None:
                # attach_socket retains its HTTP response on the SocketIO.
                # Close that owner first; otherwise HTTPResponse later flushes
                # an already-closed buffer during collection on Python 3.13.
                response = getattr(stream, "_response", None)
                if response is not None:
                    response.close()
                stream.close()
        finally:
            self.client.close()
