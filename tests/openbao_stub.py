"""Stand-ins for the `bao` CLI and `kubectl` used by tests/test_openbao_bootstrap.py.

The same file is installed twice on a temporary PATH: called as `bao` it emulates
the OpenBao commands scripts/openbao-bootstrap.sh uses, keeping state in the
JSON file named by BAO_STUB_STATE; called as `kubectl` it lists server pods,
answers waits and secret reads, and runs `exec ... -- CMD` locally so the
in-pod `sh -c ... bao` wrapper reaches the stub `bao`. Every call is appended
to BAO_STUB_LOG as JSON: argv, the pod, whether a token was presented, and the
size of stdin (never its content).
"""

import base64
import json
import os
import secrets
import subprocess
import sys

STATE = os.environ["BAO_STUB_STATE"]
LOG = os.environ["BAO_STUB_LOG"]
CA_PEM = "-----BEGIN CERTIFICATE-----\nTUlJQ2Zha2U=\n-----END CERTIFICATE-----\n"


def load():
    with open(STATE) as handle:
        return json.load(handle)


def save(state):
    with open(STATE, "w") as handle:
        json.dump(state, handle)


def log(entry):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(entry) + "\n")


def fail(message, code=2):
    sys.stderr.write(message + "\n")
    sys.exit(code)


def kubectl(argv):
    args = list(argv)
    while args and args[0] in ("--context", "-n"):
        args = args[2:]
    log({"tool": "kubectl", "argv": argv})
    if args[:2] == ["get", "statefulset"]:
        print(len(load()["pods"]), end="")
        return 0
    if args[:2] == ["get", "pods"]:
        state = load()
        running = state.get("running", state["pods"]) if "--field-selector=status.phase=Running" in args else state["pods"]
        print("\n".join(running))
        return 0
    if args[:1] == ["wait"]:
        return 0
    if args[:2] == ["get", "secret"]:
        print(base64.b64encode(CA_PEM.encode()).decode())
        return 0
    if args[:1] == ["exec"]:
        rest = args[1:]
        pod = next(a for a in rest if not a.startswith("-") and a not in ("openbao",))
        command = rest[rest.index("--") + 1:]
        if "exec bao" not in " ".join(command):
            return 0  # e.g. reload-tls signals the server; nothing to emulate
        env = dict(os.environ, BAO_STUB_POD=pod)
        env.pop("BAO_TOKEN", None)
        return subprocess.run(command, env=env).returncode
    fail(f"kubectl stub: unsupported {argv}")


def parse_fields(items, stdin):
    data = {}
    for item in items:
        key, value = item.split("=", 1)
        if value == "-":
            value = stdin
        elif value.startswith("@"):
            with open(value[1:]) as handle:
                value = handle.read()
        data[key] = value
    return data


def bao(argv):
    state = load()
    pod = os.environ.get("BAO_STUB_POD", "external")
    token = os.environ.get("BAO_TOKEN", "")
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    log({"tool": "bao", "pod": pod, "argv": argv, "token": bool(token), "stdin_bytes": len(stdin),
         "addr": os.environ.get("BAO_ADDR", "")})
    sealed = state["sealed"].get(pod, False) if pod != "external" else False
    flags = [a for a in argv if a.startswith("-") and a != "-"]
    words = [a for a in argv if not a.startswith("-") or a == "-"]

    if words[:1] == ["status"]:
        print(json.dumps({"initialized": state["initialized"] and (pod == state["pods"][0] or not sealed or state["joined"].get(pod, False)),
                          "sealed": sealed}))
        return 2 if sealed else 0
    if words[:2] == ["operator", "init"]:
        if state["initialized"]:
            fail("Error initializing: Vault is already initialized")
        shares = int(next(f for f in flags if f.startswith("-key-shares=")).split("=")[1])
        threshold = int(next(f for f in flags if f.startswith("-key-threshold=")).split("=")[1])
        keys = [f"unseal-share-{i}-" + secrets.token_hex(8) for i in range(shares)]
        state.update(initialized=True, keys=keys, threshold=threshold, root="root-token-" + secrets.token_hex(8))
        save(state)
        print(json.dumps({"unseal_keys_b64": keys, "unseal_keys_hex": keys, "unseal_shares": shares,
                          "unseal_threshold": threshold, "root_token": state["root"]}))
        return 0
    if words[:2] == ["write", "sys/unseal"]:
        if stdin not in state.get("keys", []):
            fail("Error unsealing: invalid key")
        progress = state["progress"].get(pod, []) + [stdin]
        state["progress"][pod] = progress
        if len(set(progress)) >= state["threshold"]:
            state["sealed"][pod] = False
            state["joined"][pod] = True
            state["progress"][pod] = []
        save(state)
        print("{}")
        return 0

    # Everything below needs an unsealed server and the root token.
    if sealed:
        fail("Error: Vault is sealed")
    if token != state.get("root"):
        fail("Error: permission denied")
    objects = state["objects"]

    if words[:2] in (["secrets", "list"], ["auth", "list"]):
        print(json.dumps(state["mounts" if words[0] == "secrets" else "auths"]))
        return 0
    if words[:2] in (["secrets", "enable"], ["auth", "enable"]):
        path = next(f for f in flags if f.startswith("-path=")).split("=", 1)[1] + "/"
        version = next((f.split("=")[1] for f in flags if f.startswith("-version=")), None)
        table = state["mounts" if words[0] == "secrets" else "auths"]
        if path in table:
            fail(f"Error: path is already in use at {path}")
        table[path] = {"type": words[2], "options": {"version": version} if version else None}
        save(state)
        return 0
    if words[:1] == ["read"]:
        path = words[1]
        if path not in objects:
            fail(f"No value found at {path}")
        print(json.dumps({"data": objects[path]}))
        return 0
    if words[:1] == ["write"]:
        path, fields = words[1], parse_fields(words[2:], stdin)
        if "/role/" in path:
            for name in ("bound_service_account_names", "bound_service_account_namespaces", "token_policies"):
                fields[name] = fields[name].split(",")
            fields["token_ttl"] = int(fields["token_ttl"])
            fields["token_max_ttl"] = int(fields["token_max_ttl"])
        if "connection_url" in fields:
            fields = {"plugin_name": fields["plugin_name"], "allowed_roles": fields["allowed_roles"].split(","),
                      "connection_details": {"connection_url": fields["connection_url"], "username": fields["username"],
                                             "max_open_connections": 4},
                      "password_set": bool(fields.get("password"))}
        if "/roles/" in path:
            fields = {"db_name": fields["db_name"], "creation_statements": [fields["creation_statements"]],
                      "default_ttl": 3600 if fields["default_ttl"] == "1h" else fields["default_ttl"],
                      "max_ttl": 86400 if fields["max_ttl"] == "24h" else fields["max_ttl"]}
        if "/keys/" in path:
            fields = {"type": fields.get("type", "aes256-gcm96"), "derived": fields.get("derived") == "true"}
        if "token_reviewer_jwt" in fields:
            fields["token_reviewer_jwt_set"] = bool(fields.pop("token_reviewer_jwt"))
        objects[path] = fields
        save(state)
        return 0
    if words[:2] == ["policy", "write"]:
        objects[f"sys/policies/acl/{words[2]}"] = {"name": words[2], "policy": stdin}
        save(state)
        return 0
    if words[:1] == ["kv"]:
        mount = next(f for f in flags if f.startswith("-mount=")).split("=", 1)[1]
        path = f"{mount}/{words[2]}"
        kv = state["kv"]
        if words[1] == "get":
            if path not in kv:
                fail(f"No value found at {mount}/data/{words[2]}")
            print(json.dumps({"data": {"data": kv[path], "metadata": {"version": 1}}}))
            return 0
        fields = parse_fields(words[3:], stdin)
        if words[1] == "put":
            kv[path] = fields
        elif words[1] == "patch":
            if path not in kv:
                fail("Error: no existing secret to patch")
            kv[path].update(fields)
        save(state)
        return 0
    fail(f"bao stub: unsupported {argv}")


if __name__ == "__main__":
    name = os.path.basename(sys.argv[0])
    sys.exit(kubectl(sys.argv[1:]) if name == "kubectl" else bao(sys.argv[1:]))
