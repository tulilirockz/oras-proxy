# My idea: read registry, expose `$API/registry/`, with `$API/registry/component.raw` or whateve dependingo n the tag

# Example:
# Path=http://localhost:6767/ghcr.io_tulilirockz_zirconium/ # this would have valid! (usr-VERSION-WHATEVER.raw) files based off of tags in the registry itself
# Path=https://1270333429.rsc.cdn77.org/nightly/sysupdate/
# MatchPattern=gnomeos-nvidia-%a_@v.addon.efi gnomeos-nvidia-%a_@v.addon.efi.xz

# Get the URL (ghcr.io/tulilirockz/zirconium/usr:latest), resolve to blob, oras blob fetch to file descriptor. bam done
import platform
import json
import subprocess
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

app = FastAPI()

@app.get('/{_:path}', response_class=StreamingResponse)
def main(request: Request):
    registry_image = request.url.path[1:] if not request.url.query else request.url.path[1:] + "?" + request.url.query
    architecture_uname_raw = platform.uname().machine
    github_architecture = architecture_uname_raw.replace("x86_", "amd").replace("arm", "aarch")

    manifest = subprocess.run(["oras", "manifest", "fetch", registry_image, "--platform", f"linux/{github_architecture}"], capture_output=True)
    if manifest.stdout == b'':
        manifest = subprocess.run(["oras", "manifest", "fetch", registry_image], capture_output=True)

    manifest_config = json.loads(manifest.stdout)

    # {"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","artifactType":"application/vnd.unknown.artifact.v1","config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"sha256:9d99a75171aea000c711b34c0e5e3f28d3d537dd99d110eafbfbc2bd8e52c2bf","size":37},"layers":[{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:a1a242c85fca5219717a895aa5aa727e76a85612fd58646a3720414704a6b7c9","size":419430400,"annotations":{"org.opencontainers.image.title":"Zirconium_20260708005028_x86-64.usr-x86-64-verity.959684452ba1bef9bb4933d8eaafab57.raw"}},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:a8907ad0fbeddeec5f7018797f0009f521a686d33b0c8e17787c261ac56b25b8","size":1730,"annotations":{"org.opencontainers.image.title":"Zirconium_20260708005028_x86-64.usr-x86-64-verity-sig.85b79c7cfe4849c7b34aee2d9ae0973d.raw"}},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:d4c9faf77be8559b445b97316619b365d7e5ef8e0de1630fdc6351f9b83797d8","size":3645689856,"annotations":{"org.opencontainers.image.title":"Zirconium_20260708005028_x86-64.usr-x86-64.6e3a4113bb8b6d1eebf5e0df64e5328e.raw"}}],"annotations":{"org.opencontainers.image.created":"2026-07-08T00:57:30Z"}}

    systemd_formatted_arch = architecture_uname_raw.replace("_", "-")
    filter_image = lambda x: x["annotations"]["org.opencontainers.image.title"] == f"Zirconium_20260708005028_{systemd_formatted_arch}.usr-{systemd_formatted_arch}.6e3a4113bb8b6d1eebf5e0df64e5328e.raw" 
    filter_nothing = lambda x: x

    selected_artifact = list(filter(filter_nothing, manifest_config["layers"]))[0]["digest"]

    # f = open("chugngus.raw", 'w')
    # filedesc = f.fileno()
    # process = subprocess.run(["oras", "blob", "fetch", f"ghcr.io/tulilirockz/zirconium/usr:latest@{selected_artifact}", "--no-tty", "-o", "/dev/stdout"], stdout=subprocess.PIPE)
    with subprocess.Popen(["oras", "blob", "fetch", f"{registry_image}@{selected_artifact}", "--no-tty", "-o", "/dev/stdout"], stdout=subprocess.PIPE, bufsize=1) as process:
        while True:
            c = process.stdout.read(5000)
            if not c:
                break
            yield c

