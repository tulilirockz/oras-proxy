#!/usr/bin/env python3
"""Generate or incrementally update a systemd-sysupdate SHA256SUMS file.

The manifest is built from the OCI artifacts in one or more registry
repositories.  Every layer's SHA256 is taken from its manifest digest, so no
image/blob data is ever downloaded.  Only the (small) manifests are fetched.

The output is append-only by default: entries already present in an existing
SHA256SUMS file are kept, and new artifacts are appended.  This means a
workflow run does not have to re-hash every image that was ever published.

Example:

    scripts/sha256sums.py \
        --repositories-file repositories.txt \
        --existing SHA256SUMS \
        --output SHA256SUMS \
        --mapping-output SHA256SUMS.json

The optional mapping file records which repository/tag each filename came
from, so a proxy can resolve a sysupdate request without guessing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
ATTESTATION_MEDIA_TYPES = {
    "application/vnd.in-toto+json",
}
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SUMS_LINE_RE = re.compile(r"^(?P<digest>[0-9a-f]{64}) [ *](?P<name>.+)$")

# `snapd-arm64_nightly.20261001.0.raw.zst`
#   -> component=snapd, arch=arm64, version=nightly.20261001.0
DEFAULT_NAME_PATTERN = (
    r"^(?P<component>.+)-(?P<arch>[^-]+)_(?P<version>.+?)"
    r"\.raw(?:\.(?P<compression>[a-z0-9]+))?$"
)
DEFAULT_TAG_TEMPLATE = "{version}-{arch}"
DEFAULT_FILENAME_REGEX = r"\.(?:raw|efi|tar)(?:\.(?:zst|xz|gz))?$"


class CraneError(RuntimeError):
    """`crane` failed or returned something that was not JSON."""


class ManifestError(RuntimeError):
    """An OCI manifest did not look like something we can use."""


@dataclass(frozen=True)
class Entry:
    name: str
    sha256: str
    repository: str
    tag: str
    size: int = 0

    @property
    def digest(self) -> str:
        return f"sha256:{self.sha256}"

    def sums_line(self) -> str:
        return f"{self.sha256}  {self.name}"

    def mapping(self) -> dict[str, str | int]:
        return {
            "sha256": self.sha256,
            "digest": self.digest,
            "repository": self.repository,
            "tag": self.tag,
            "size": self.size,
        }


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise CraneError(f"{' '.join(cmd)}: {detail}")
    return proc.stdout


def crane_ls(crane: str, repository: str) -> list[str]:
    output = run([crane, "ls", repository])
    return [line.strip() for line in output.splitlines() if line.strip()]


def crane_manifest(crane: str, ref: str) -> dict:
    output = run([crane, "manifest", ref])
    try:
        manifest = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CraneError(f"crane manifest {ref} returned invalid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CraneError(f"crane manifest {ref} returned a non-object manifest")
    return manifest


def validate_entry_name(name: str) -> None:
    """Reject names that sysupdate refuses to consume."""
    if not name or name.startswith("/"):
        raise ManifestError(f"invalid manifest entry name: {name!r}")
    if "%" in name:
        raise ManifestError(f"manifest entry name contains '%': {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ManifestError(f"manifest entry name is not normalized: {name!r}")


def entries_from_manifest(
    repository: str,
    tag: str,
    manifest: dict,
    filename_re: re.Pattern[str],
) -> list[Entry]:
    entries: list[Entry] = []
    for layer in manifest.get("layers") or []:
        annotations = layer.get("annotations") or {}
        name = annotations.get("org.opencontainers.image.title")
        if not name or not filename_re.search(name):
            continue

        digest = layer.get("digest", "")
        if not DIGEST_RE.match(digest):
            raise ManifestError(
                f"{repository}:{tag}: layer {name!r} has unsupported digest {digest!r}"
            )

        validate_entry_name(name)
        entries.append(
            Entry(
                name=name,
                sha256=digest.split(":", 1)[1],
                repository=repository,
                tag=tag,
                size=int(layer.get("size") or 0),
            )
        )
    return entries


def entries_for_tag(
    repository: str,
    tag: str,
    fetch_manifest,
    filename_re: re.Pattern[str],
) -> list[Entry]:
    top = fetch_manifest(f"{repository}:{tag}")

    if "manifests" in top or top.get("mediaType") in INDEX_MEDIA_TYPES:
        descriptors = top.get("manifests") or []
    else:
        descriptors = [{"digest": None}]

    entries: list[Entry] = []
    for descriptor in descriptors:
        if (
            (descriptor.get("annotations") or {}).get("vnd.docker.reference.type")
            == "attestation-manifest"
        ):
            continue
        if descriptor.get("artifactType") in ATTESTATION_MEDIA_TYPES:
            continue

        digest = descriptor.get("digest")
        manifest = fetch_manifest(f"{repository}@{digest}") if digest else top
        if not manifest.get("layers"):
            continue
        entries.extend(entries_from_manifest(repository, tag, manifest, filename_re))

    return entries


def read_repositories(args: argparse.Namespace) -> list[str]:
    repositories = list(args.repository)
    for filename in args.repositories_file:
        try:
            text = Path(filename).read_text()
        except OSError as exc:
            raise SystemExit(f"cannot read repository list {filename}: {exc}") from exc
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                repositories.append(line)

    seen: set[str] = set()
    result: list[str] = []
    for repository in repositories:
        repository = repository.rstrip("/")
        if repository and repository not in seen:
            seen.add(repository)
            result.append(repository)
    if not result:
        raise SystemExit("no repositories were provided")
    return result


def collect_entries(
    args: argparse.Namespace,
    repositories: list[str],
) -> tuple[dict[str, Entry], int]:
    filename_re = re.compile(args.filename_regex)
    tag_re = re.compile(args.tag_regex)
    exclude_re = re.compile(args.exclude_tag_regex) if args.exclude_tag_regex else None
    fetch_manifest = lambda ref: crane_manifest(args.crane, ref)  # noqa: E731

    entries: dict[str, Entry] = {}
    failures: list[str] = []
    conflicts: list[str] = []
    tags_seen = 0

    with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
        futures = {}
        for repository in repositories:
            try:
                tags = crane_ls(args.crane, repository)
            except CraneError as exc:
                failures.append(str(exc))
                warn(str(exc))
                continue

            selected = [
                tag
                for tag in sorted(tags)
                if tag_re.search(tag) and not (exclude_re and exclude_re.search(tag))
            ]
            tags_seen += len(selected)
            for tag in selected:
                future = pool.submit(
                    entries_for_tag, repository, tag, fetch_manifest, filename_re
                )
                futures[future] = (repository, tag)

        for future in as_completed(futures):
            repository, tag = futures[future]
            try:
                found = future.result()
            except (CraneError, ManifestError, ValueError) as exc:
                failures.append(str(exc))
                warn(str(exc))
                continue

            for entry in found:
                previous = entries.get(entry.name)
                if previous is None:
                    entries[entry.name] = entry
                elif previous.sha256 != entry.sha256:
                    conflicts.append(
                        f"{entry.name}: {previous.repository}:{previous.tag} has "
                        f"{previous.sha256}, but {entry.repository}:{tag} has "
                        f"{entry.sha256}"
                    )
                elif previous.repository != entry.repository:
                    # Same bytes in two repositories; keep the first mapping.
                    warn(
                        f"{entry.name}: also present in {entry.repository}:{tag}; "
                        f"keeping {previous.repository}:{previous.tag}"
                    )

    if conflicts:
        raise SystemExit(
            "the same filename was found with different hashes:\n  "
            + "\n  ".join(sorted(conflicts))
            + "\nUse --overwrite only if you really intend to replace the published file."
        )

    if failures and args.strict:
        raise SystemExit(
            "failed to scan some repositories or tags:\n  " + "\n  ".join(failures)
        )

    return entries, tags_seen


def parse_sums(text: str) -> tuple[dict[str, str], list[str]]:
    entries: dict[str, str] = {}
    extra: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        match = SUMS_LINE_RE.match(raw_line)
        if match:
            entries[match.group("name")] = match.group("digest")
        else:
            extra.append(raw_line)
    return entries, extra


def load_mapping(path: Path | None) -> dict[str, dict]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"could not read existing mapping {path}: {exc}")
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def merge_entries(
    existing: dict[str, str],
    generated: dict[str, Entry],
    *,
    overwrite: bool,
    prune: bool,
    existing_mapping: dict[str, dict],
    scanned_repositories: set[str],
) -> tuple[dict[str, str], int, int, int]:
    merged = dict(existing)
    added = updated = 0

    for name, entry in generated.items():
        previous = merged.get(name)
        if previous is None:
            merged[name] = entry.sha256
            added += 1
        elif previous != entry.sha256:
            if not overwrite:
                raise SystemExit(
                    f"refusing to replace {name}: existing SHA256 is {previous}, "
                    f"new SHA256 is {entry.sha256} (use --overwrite to replace)"
                )
            merged[name] = entry.sha256
            updated += 1

    removed = 0
    if prune:
        if not existing_mapping:
            raise SystemExit("--prune requires an existing mapping file")
        for name in list(merged):
            if name in generated:
                continue
            mapping = existing_mapping.get(name) or {}
            if mapping.get("repository") in scanned_repositories:
                del merged[name]
                removed += 1

    return merged, added, updated, removed


def build_mapping(
    generated: dict[str, Entry],
    existing_mapping: dict[str, dict],
    *,
    prune: bool,
    scanned_repositories: set[str],
) -> dict:
    entries = dict(existing_mapping)
    for name, entry in generated.items():
        entries[name] = entry.mapping()

    if prune:
        for name in list(entries):
            if name in generated:
                continue
            if (entries[name] or {}).get("repository") in scanned_repositories:
                del entries[name]

    return {"version": 1, "entries": dict(sorted(entries.items()))}


def check_names(
    generated: dict[str, Entry],
    *,
    name_pattern: str,
    tag_template: str,
    repository_base: str | None,
) -> list[str]:
    pattern = re.compile(name_pattern)
    problems: list[str] = []

    for name, entry in generated.items():
        match = pattern.match(name)
        if not match:
            problems.append(f"{name}: does not match --name-pattern")
            continue

        fields = match.groupdict()
        try:
            expected_tag = tag_template.format(**fields)
        except KeyError as exc:
            problems.append(f"{name}: --tag-template references unknown field {exc}")
            continue
        if expected_tag != entry.tag:
            problems.append(
                f"{name}: expected tag {expected_tag!r} from --tag-template, "
                f"but the artifact is tagged {entry.tag!r}"
            )

        if repository_base:
            component = fields.get("component")
            expected_repository = f"{repository_base.rstrip('/')}/{component}"
            if expected_repository != entry.repository:
                problems.append(
                    f"{name}: expected repository {expected_repository!r}, "
                    f"but the artifact lives in {entry.repository!r}"
                )

    return problems


def write_sums(path: Path, entries: dict[str, str], extra_lines: list[str]) -> None:
    lines = list(extra_lines)
    lines.extend(f"{digest}  {name}" for name, digest in sorted(entries.items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or incrementally update a systemd-sysupdate SHA256SUMS file.",
    )
    parser.add_argument(
        "--repository",
        action="append",
        default=[],
        metavar="REPO",
        help="Registry repository to scan (repeatable), e.g. ghcr.io/org/image/usr",
    )
    parser.add_argument(
        "--repositories-file",
        action="append",
        default=[],
        metavar="FILE",
        help="File with one repository per line (repeatable, '#' comments allowed)",
    )
    parser.add_argument(
        "--existing", type=Path, help="Existing SHA256SUMS to merge into"
    )
    parser.add_argument(
        "--existing-mapping",
        type=Path,
        help="Existing SHA256SUMS.json mapping to merge into",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Where to write SHA256SUMS"
    )
    parser.add_argument(
        "--mapping-output",
        type=Path,
        help="Where to write the optional filename -> repository/tag mapping",
    )
    parser.add_argument(
        "--crane", default="crane", help="Path to the crane binary (default: crane)"
    )
    parser.add_argument(
        "--tag-regex",
        default=".*",
        help="Only scan tags matching this regular expression (default: all)",
    )
    parser.add_argument(
        "--exclude-tag-regex",
        default="",
        help="Skip tags matching this regular expression",
    )
    parser.add_argument(
        "--filename-regex",
        default=DEFAULT_FILENAME_REGEX,
        help=f"Only include layers whose title matches this (default: {DEFAULT_FILENAME_REGEX})",
    )
    parser.add_argument(
        "--name-pattern",
        default=DEFAULT_NAME_PATTERN,
        help="Pattern used by --check-names to parse published filenames",
    )
    parser.add_argument(
        "--tag-template",
        default=DEFAULT_TAG_TEMPLATE,
        help="Tag template used by --check-names (default: {version}-{arch})",
    )
    parser.add_argument(
        "--repository-base",
        default="",
        help="Expected repository prefix used by --check-names, e.g. ghcr.io/org/image",
    )
    parser.add_argument(
        "--check-names",
        action="store_true",
        help="Warn when filenames do not follow the expected name/tag convention",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing entry when its hash changed",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove entries for scanned repositories that no longer exist",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail if any repository or tag could not be scanned",
    )
    parser.add_argument(
        "--parallelism",
        type=int,
        default=8,
        help="Number of manifests to fetch concurrently (default: 8)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repositories = read_repositories(args)
    scanned_repositories = set(repositories)

    generated, tags_seen = collect_entries(args, repositories)

    existing_entries: dict[str, str] = {}
    extra_lines: list[str] = []
    if args.existing and args.existing.exists():
        existing_entries, extra_lines = parse_sums(args.existing.read_text())

    existing_mapping = load_mapping(args.existing_mapping)

    merged, added, updated, removed = merge_entries(
        existing_entries,
        generated,
        overwrite=args.overwrite,
        prune=args.prune,
        existing_mapping=existing_mapping,
        scanned_repositories=scanned_repositories,
    )

    if args.check_names:
        problems = check_names(
            generated,
            name_pattern=args.name_pattern,
            tag_template=args.tag_template,
            repository_base=args.repository_base or None,
        )
        for problem in problems:
            warn(problem)
        if problems and args.strict:
            raise SystemExit("some filenames do not follow the expected convention")

    write_sums(args.output, merged, extra_lines)
    if args.mapping_output:
        write_json(
            args.mapping_output,
            build_mapping(
                generated,
                existing_mapping,
                prune=args.prune,
                scanned_repositories=scanned_repositories,
            ),
        )

    print(
        f"repositories={len(repositories)} tags={tags_seen} entries={len(merged)} "
        f"added={added} updated={updated} removed={removed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
