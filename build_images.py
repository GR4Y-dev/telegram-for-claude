"""Build changed sources only, then start Compose without implicit rebuilds.

State is kept inside .git. No environment values or credentials are persisted.
Call under update.sh's shared lock. Never prune other applications' images/cache.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def fingerprint(root, build):
    digest = hashlib.sha256()
    digest.update(json.dumps(build, sort_keys=True).encode())
    context = build.get("context", ".")
    if "://" in context:
        # Only pinned remote contexts are reproducible.
        if "#" not in context or context.endswith(("#main", "#master")):
            raise ValueError("Remote Docker build context must be pinned")
        return digest.hexdigest()
    folder = (root / context).resolve()
    repo = Path(run("git", "-C", str(root), "rev-parse", "--show-toplevel"))
    # Hash tracked build inputs, never .env, local data or the rest of the project.
    names = run("git", "-C", str(repo), "ls-files", "-z", "--", str(folder)).split("\0")
    for name in sorted(filter(None, names)):
        path = repo / name
        digest.update(name.encode())
        if path.is_symlink():
            raise ValueError("Symlink in Docker context requires manual review")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def image_id(image):
    result = subprocess.run(["docker", "image", "inspect", "--format={{.Id}}", image],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def main(root):
    # Compose config can contain secrets. Capture it; never print or save it.
    config = json.loads(run("docker", "compose", "config", "--format", "json"))
    state = Path(run("git", "rev-parse", "--git-path", "connector-builds.json")).resolve()
    try:
        saved = json.loads(state.read_text())
    except (OSError, ValueError):
        saved = {}
    updated = {}
    for service, spec in config["services"].items():
        if not spec.get("build"):
            continue
        build = spec["build"]
        build = {"context": build} if isinstance(build, str) else build
        source = fingerprint(root, build)
        image = spec.get("image") or f"{config['name']}-{service}"
        current = image_id(image)
        if not current or saved.get(service) != {"source": source, "image": current}:
            subprocess.run(["docker", "compose", "build", service], check=True)
            current = image_id(image)
            if not current:
                raise RuntimeError("Docker did not produce the requested image")
        updated[service] = {"source": source, "image": current}
    subprocess.run(["docker", "compose", "up", "-d", "--no-build"], check=True)
    temporary = state.with_suffix(".tmp")
    temporary.write_text(json.dumps(updated), encoding="utf-8")
    temporary.replace(state)


if __name__ == "__main__":
    main(Path.cwd())
