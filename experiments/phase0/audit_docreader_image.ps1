<#
.SYNOPSIS
    P0-T09R — read-only inventory of the DocReader container image, emitted as JSON.

.DESCRIPTION
    Enters the image with `docker run --rm --entrypoint sh <image> -c '...'` and
    collects, without mutating the image or the workspace:

      * host-side image identity   : Id / RepoDigests / Size / Created / Arch / Os / Config
      * registry manifest digest   : index + per-platform manifest, hashed from raw bytes
      * dpkg packages              : name, version, status, arch
      * /usr/share/doc/*/copyright : sha256 + DEP-5 header lines (`License:`, `Source:`, ...)
      * Python distributions       : version, License-Expression / License / Classifiers,
                                     PEP 639 `License-File` payloads with sha256
      * OCR weights                : every `*.onnx` and every path matching `*rapidocr*`,
                                     with byte size and sha256

    The payload executed inside the container is a Python program base64-encoded on
    this host and piped into `docker run -i`; the container itself only reads.

    Nothing is written inside the repository. The JSON goes to stdout unless
    -OutFile is given (the caller decides where the artefact lands).

.PARAMETER Image
    Image reference, default `wechatopenai/weknora-docreader:v0.8.3`.

.PARAMETER OutFile
    Optional path for the JSON document. Parent directory must already exist.
    Relative paths resolve against the current location.

.PARAMETER NoPull
    Fail instead of pulling when the image is not present locally.

.PARAMETER SkipRegistry
    Skip the registry manifest-digest resolution (useful when the accelerator is down).

.PARAMETER RegistryBase
    Registry/accelerator that serves `v2/` manifest bytes. Default is the one that
    is known to answer for this image in this environment: `https://docker.1panel.live`.

.EXAMPLE
    pwsh -File experiments/phase0/audit_docreader_image.ps1 -OutFile $env:TEMP\inv.json

.NOTES
    Exit codes: 0 = inventory produced, 1 = precondition failed, 2 = container audit
    failed, 3 = JSON assembly failed. All evidence travels on stderr so stdout stays
    machine-parseable.
#>
[CmdletBinding()]
param(
    [string]$Image = 'wechatopenai/weknora-docreader:v0.8.3',
    [string]$OutFile,
    [switch]$NoPull,
    [switch]$SkipRegistry,
    [string]$RegistryBase = 'https://docker.1panel.live'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:StartedAt = [DateTimeOffset]::UtcNow
$script:Steps = [System.Collections.Generic.List[object]]::new()

function Write-Step {
    param([string]$Name, [int]$ExitCode, [string]$Detail)
    $script:Steps.Add([ordered]@{
            name   = $Name
            exit   = $ExitCode
            detail = $Detail
        })
    Write-Host "[audit] $Name -> exit $ExitCode $(if ($Detail) { "| $Detail" })" -ForegroundColor DarkGray
}

function Invoke-Native {
    <#  Runs a native command, returns exit code + stdout/stderr without throwing. #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$StdIn
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    foreach ($a in $Arguments) { $psi.ArgumentList.Add($a) }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $psi.UseShellExecute = $false
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $p = [System.Diagnostics.Process]::Start($psi)
    if (-not [string]::IsNullOrEmpty($StdIn)) { $p.StandardInput.Write($StdIn) }
    $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEnd()
    $err = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    return [pscustomobject]@{ ExitCode = $p.ExitCode; StdOut = $out; StdErr = $err }
}

# --------------------------------------------------------------------------- #
# 0. preconditions
# --------------------------------------------------------------------------- #
try {
    $v = Invoke-Native -FilePath 'docker' -Arguments @('version', '--format', '{{.Server.Version}}')
    if ($v.ExitCode -ne 0) { throw "docker daemon unreachable: $($v.StdErr.Trim())" }
    Write-Step -Name 'docker-version' -ExitCode 0 -Detail ("server " + $v.StdOut.Trim())
}
catch {
    Write-Error "docker is not usable: $($_.Exception.Message)" -ErrorAction Continue
    exit 1
}

$dockerVersion = $v.StdOut.Trim()

# --------------------------------------------------------------------------- #
# 1. image presence + pull (timed)
# --------------------------------------------------------------------------- #
$pull = [ordered]@{ performed = $false; seconds = $null; exit = $null; already_local = $false }
$t0 = [DateTimeOffset]::UtcNow
$insp = Invoke-Native -FilePath 'docker' -Arguments @('image', 'inspect', $Image)
if ($insp.ExitCode -eq 0) {
    $pull.already_local = $true
    Write-Step -Name 'image-present' -ExitCode 0 -Detail 'already local, no pull'
}
else {
    if ($NoPull) {
        Write-Error "image '$Image' not present locally and -NoPull was set" -ErrorAction Continue
        exit 1
    }
    $pr = Invoke-Native -FilePath 'docker' -Arguments @('pull', $Image)
    $pull.performed = $true
    $pull.exit = $pr.ExitCode
    $pull.seconds = [math]::Round(([DateTimeOffset]::UtcNow - $t0).TotalSeconds, 3)
    if ($pr.ExitCode -ne 0) {
        Write-Error ("docker pull failed (exit $($pr.ExitCode)): " + $pr.StdErr.Trim()) -ErrorAction Continue
        exit 1
    }
    Write-Step -Name 'docker-pull' -ExitCode 0 -Detail ("$($pull.seconds)s")
}

# --------------------------------------------------------------------------- #
# 2. host-side image metadata
# --------------------------------------------------------------------------- #
$rawInspect = Invoke-Native -FilePath 'docker' -Arguments @('image', 'inspect', $Image)
if ($rawInspect.ExitCode -ne 0) {
    Write-Error "docker image inspect failed: $($rawInspect.StdErr.Trim())" -ErrorAction Continue
    exit 1
}
$img = ($rawInspect.StdOut | ConvertFrom-Json)[0]
Write-Step -Name 'image-inspect' -ExitCode 0 -Detail ("id " + $img.Id.Substring(0, 19) + "…")

$imageBlock = [ordered]@{
    reference      = $Image
    id             = $img.Id
    repo_digests   = @($img.RepoDigests)
    size_bytes        = [int64]$img.Size
    # Docker 29 removed the deprecated `.VirtualSize`; `.Size` is the single
    # figure `docker image inspect` reports now, so it is emitted once and the
    # provenance of the number is recorded instead of inventing a second one.
    size_bytes_source = 'image.Size (Docker 29 no longer exposes .VirtualSize)'
    created        = $img.Created
    architecture   = $img.Architecture
    os             = $img.Os
    layer_count    = @($img.RootFS.Layers).Count
    rootfs_diff_ids = @($img.RootFS.Layers)
    entrypoint     = @($img.Config.Entrypoint)
    cmd            = @($img.Config.Cmd)
    working_dir    = $img.Config.WorkingDir
    env            = @($img.Config.Env)
    labels         = $img.Config.Labels
}

# --------------------------------------------------------------------------- #
# 3. registry manifest digests (raw-byte sha256, not re-serialised JSON)
# --------------------------------------------------------------------------- #
$registryBlock = [ordered]@{ base = $RegistryBase; attempted = $false; index = $null; platform = $null; errors = @() }

function Get-RawManifest {
    param([string]$Url, [string]$Accept)
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $client = [System.Net.Http.HttpClient]::new($handler)
    $req = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $Url)
    [void]$req.Headers.TryAddWithoutValidation('Accept', $Accept)
    $resp = $client.SendAsync($req).GetAwaiter().GetResult()
    $status = [int]$resp.StatusCode
    $bytes = $resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
    $digestHdr = $null
    if ($resp.Headers.Contains('Docker-Content-Digest')) { $digestHdr = ($resp.Headers.GetValues('Docker-Content-Digest') -join ',') }
    $mediaType = $null
    if ($null -ne $resp.Content.Headers.ContentType) { $mediaType = $resp.Content.Headers.ContentType.MediaType }
    $client.Dispose()
    return [pscustomobject]@{
        StatusCode        = $status
        Bytes             = $bytes
        Sha256            = ('sha256:' + (Get-Sha256Hex $bytes))
        DockerContentDigest = $digestHdr
        MediaType         = $mediaType
    }
}

function Get-Sha256Hex {
    param([byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes)) -replace '-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

if (-not $SkipRegistry) {
    $registryBlock.attempted = $true
    $tag = if ($Image.Contains(':')) { $Image.Split(':')[-1] } else { 'latest' }
    $repo = ($Image.Split('@')[0] -replace ':(?!.*:)', '')
    if ($Image -match '^(?<repo>[^:@]+)(:(?<tag>[^@]+))?(@(?<dgst>sha256:[0-9a-f]+))?$') {
        $repo = $Matches['repo']
        if ($Matches['tag']) { $tag = $Matches['tag'] }
    }
    $idxUrl = "$RegistryBase/v2/$repo/manifests/$tag"
    $acceptAll = 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
    try {
        $idx = Get-RawManifest -Url $idxUrl -Accept $acceptAll
        $idxJson = [System.Text.Encoding]::UTF8.GetString($idx.Bytes) | ConvertFrom-Json
        $registryBlock.index = [ordered]@{
            url                 = $idxUrl
            http_status         = $idx.StatusCode
            media_type          = $idx.MediaType
            computed_sha256     = $idx.Sha256
            docker_content_digest = $idx.DockerContentDigest
            matches_header      = ($idx.DockerContentDigest -eq $idx.Sha256)
            media_type_declared = $idxJson.mediaType
            manifests           = @()
        }
        foreach ($m in @($idxJson.manifests)) {
            $registryBlock.index.manifests += [ordered]@{
                digest       = $m.digest
                media_type   = $m.mediaType
                architecture = $m.platform.architecture
                os           = $m.platform.os
                variant      = if ($m.platform.PSObject.Properties.Name -contains 'variant') { $m.platform.variant } else { $null }
            }
        }
        Write-Step -Name 'registry-index' -ExitCode 0 -Detail $idx.Sha256

        $plat = @($idxJson.manifests | Where-Object { $_.platform.architecture -eq 'amd64' -and $_.platform.os -eq 'linux' }) | Select-Object -First 1
        if ($null -ne $plat) {
            $pUrl = "$RegistryBase/v2/$repo/manifests/$($plat.digest)"
            $pm = Get-RawManifest -Url $pUrl -Accept 'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
            $registryBlock.platform = [ordered]@{
                url                   = $pUrl
                http_status           = $pm.StatusCode
                media_type            = $pm.MediaType
                computed_sha256       = $pm.Sha256
                docker_content_digest = $pm.DockerContentDigest
                matches_declared      = ($pm.Sha256 -eq $plat.digest)
                claimed_digest        = $plat.digest
            }
            Write-Step -Name 'registry-platform' -ExitCode 0 -Detail $pm.Sha256
        }
    }
    catch {
        $registryBlock.errors += $_.Exception.Message
        Write-Step -Name 'registry-manifest' -ExitCode 1 -Detail $_.Exception.Message
    }
}

# --------------------------------------------------------------------------- #
# 4. in-container read-only inventory (python payload, base64 over stdin)
# --------------------------------------------------------------------------- #
$payload = @'
import hashlib, json, os, re, subprocess, sys, traceback

META_KEYS = ("License", "License-Expression", "License-File")
COPY_KEYS = ("Format", "Upstream-Name", "Upstream-Contact", "Source", "Comment", "License", "Files", "Copyright")
COPY_RE = re.compile(r"^(" + "|".join(COPY_KEYS) + r"):\s*(.*)$")


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return {"cmd": " ".join(cmd), "exit": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except Exception as exc:  # noqa: BLE001
        return {"cmd": " ".join(cmd), "exit": None, "stdout": "", "stderr": str(exc)}


def sha256_file(path, limit=None):
    h = hashlib.sha256()
    n = 0
    try:
        with open(path, "rb") as fh:
            while True:
                b = fh.read(1 << 20)
                if not b:
                    break
                h.update(b)
                n += len(b)
                if limit is not None and n >= limit:
                    break
        return h.hexdigest(), n
    except OSError as exc:
        return "ERROR:" + str(exc), None


def read_text(path, cap=1 << 20):
    try:
        with open(path, "rb") as fh:
            raw = fh.read(cap)
        return raw.decode("utf-8", "replace")
    except OSError:
        return None


def parse_copyright(pkg, path):
    entry = {"package": pkg, "path": path, "exists": False}
    real = os.path.realpath(path)
    if not os.path.exists(real):
        return entry
    entry["exists"] = True
    if real != path:
        entry["resolved_from"] = real
    st = os.stat(real)
    entry["size"] = st.st_size
    entry["sha256"] = sha256_file(real)[0]
    text = read_text(real) or ""
    fmt = None
    for line in text.splitlines():
        m = COPY_RE.match(line)
        if m and m.group(1) == "Format":
            fmt = m.group(2).strip()
            break
    entry["format"] = fmt or ("DEP-5" if "http://www.debian.org/doc/packaging-manuals/copyright-format" in text else "freeform")
    licenses, copyrights, other = [], [], []
    for line in text.splitlines():
        m = COPY_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "License":
            name = val.strip()
            if name and name not in licenses:
                licenses.append(name[:120])
        elif key == "Copyright":
            if val and len(val) < 200:
                copyrights.append(val)
        elif key in ("Source", "Upstream-Name", "Upstream-Contact", "Comment"):
            if val:
                other.append(key + ": " + val[:200])
    entry["license_short_names"] = licenses[:60]
    entry["copyright_statements"] = copyrights[:5]
    entry["header_notes"] = other[:10]
    entry["first_lines"] = [l for l in text.splitlines() if l.strip()][:6]
    return entry


out = {"schema": "pyknora.docreader_image_inventory.container/v1"}

osrel = {}
try:
    with open("/etc/os-release") as fh:
        for line in fh:
            if "=" in line:
                k, v = line.rstrip("\n").split("=", 1)
                osrel[k] = v.strip('"')
except OSError:
    pass
out["os_release"] = osrel
out["uname"] = run(["uname", "-a"])["stdout"].strip()
out["hostname"] = run(["hostname"])["stdout"].strip()
out["dpkg_architecture"] = run(["dpkg", "--print-architecture"])["stdout"].strip()
out["nproc"] = run(["nproc"])["stdout"].strip()
out["meminfo"] = run(["sh", "-c", "head -3 /proc/meminfo"])["stdout"].strip()

pyr = run([sys.executable, "--version"])
out["python"] = {
    "version": (pyr["stdout"] + pyr["stderr"]).strip(),
    "executable": sys.executable,
    "version_info": list(sys.version_info[:3]),
    "prefix": sys.prefix,
    "sys_path": sys.path,
}

# ---- dpkg -----------------------------------------------------------------
dp = run(["dpkg-query", "-W", "-f", "${Package}\\t${Version}\\t${Status}\\t${Architecture}\\n"])
apt = []
for line in dp["stdout"].splitlines():
    parts = line.split("\t")
    if len(parts) >= 4:
        apt.append({"name": parts[0], "version": parts[1], "status": parts[2], "arch": parts[3]})
out["apt"] = {"query_exit": dp["exit"], "count": len(apt), "packages": apt}

# ---- /usr/share/doc/*/copyright -------------------------------------------
copies = []
seen = set()
for p in apt:
    pkg = p["name"]
    if pkg in seen:
        continue
    seen.add(pkg)
    copies.append(parse_copyright(pkg, "/usr/share/doc/%s/copyright" % pkg))
out["copyrights"] = {"count": len(copies), "with_file": sum(1 for c in copies if c["exists"]), "entries": copies}

# ---- python distributions -------------------------------------------------
def dist_entry(dist):
    md = dist.metadata
    name = (md["Name"] or dist.name or "").strip()
    entry = {
        "name": name,
        "version": (md["Version"] or "").strip(),
        "metadata_name": md["Name"],
        "distribution_path": str(dist._path) if hasattr(dist, "_path") else None,
        "license_expression": md.get("License-Expression"),
        "license_field": md.get("License"),
        "license_classifiers": md.get_all("Classifier") and
                               [c for c in md.get_all("Classifier") if c.startswith("License ::")],
        "all_classifiers": md.get_all("Classifier"),
        "license_files_declared": md.get_all("License-File"),
        "home_page": md.get("Home-page"),
        "project_urls": md.get_all("Project-URL"),
        "summary": md.get("Summary"),
        "requires_python": md.get("Requires-Python"),
        "metadata_sha256": sha256_file(str(dist._path / "METADATA"))[0] if hasattr(dist, "_path") and (dist._path / "METADATA").exists() else None,
    }
    lic_files = []
    base = getattr(dist, "_path", None)
    if base is not None and os.path.isdir(str(base)):
        for root, dirs, files in os.walk(str(base)):
            for f in files:
                low = f.lower()
                if low.startswith(("license", "licence", "copying", "notice")) or low.endswith((".license", ".licence")):
                    fp = os.path.join(root, f)
                    size = os.path.getsize(fp)
                    lic_files.append({"name": os.path.relpath(fp, str(base)), "size": size,
                                      "sha256": sha256_file(fp)[0]})
    entry["license_payloads"] = lic_files[:25]
    return entry

try:
    from importlib import metadata as im
    dists = {}
    for d in im.distributions():
        e = dist_entry(d)
        key = (e["name"].lower(), e["version"])
        if key not in dists:
            dists[key] = e
    pip_pkgs = sorted(dists.values(), key=lambda x: x["name"].lower())
    out["python_distributions"] = {"count": len(pip_pkgs), "packages": pip_pkgs,
                                   "error": None}
except Exception:  # noqa: BLE001
    out["python_distributions"] = {"count": 0, "packages": [], "error": traceback.format_exc()}

pipr = run([sys.executable, "-m", "pip", "list", "--format=json"])
if pipr["exit"] == 0:
    try:
        out["pip_list"] = {"exit": 0, "packages": json.loads(pipr["stdout"])}
    except Exception:  # noqa: BLE001
        out["pip_list"] = {"exit": 0, "packages": [], "parse_error": True}
else:
    out["pip_list"] = {"exit": pipr["exit"], "packages": [], "stderr": pipr["stderr"]}

# ---- model weights --------------------------------------------------------
findr = run(["find", "/", "-xdev", "(", "-name", "*.onnx", "-o", "-iname", "*rapidocr*", ")", "-type", "f"])
files = [p for p in findr["stdout"].splitlines() if p.strip()]
models = []
for fp in files:
    st = os.stat(fp) if os.path.exists(fp) else None
    models.append({
        "path": fp,
        "size": st.st_size if st else None,
        "sha256": sha256_file(fp)[0] if st else None,
        "matched_by": "*.onnx" if fp.endswith(".onnx") else ("*rapidocr*" if "rapidocr" in fp.lower() else "other"),
    })
out["models"] = {"find_exit": findr["exit"], "find_error": findr["stderr"].strip(),
                 "count": len(models), "files": models}

# ---- misc probes used by the SBOM narrative -------------------------------
out["probes"] = {
    "grpc_health_probe": run(["sh", "-c", "command -v grpc_health_probe && grpc_health_probe -version 2>&1 | head -5"])["stdout"].strip(),
    "java": run(["sh", "-c", "command -v java && java -version 2>&1 | head -3 || true"])["stdout"].strip(),
    "libreoffice": run(["sh", "-c", "command -v soffice libreoffice 2>&1 | head -3 || true"])["stdout"].strip(),
    "antiword": run(["sh", "-c", "command -v antiword && antiword -h 2>&1 | head -2 || true"])["stdout"].strip(),
    "app_dir": run(["sh", "-c", "ls -la /app 2>&1 | head -40"])["stdout"].strip(),
    "playwright_cache": run(["sh", "-c", "ls -la /root/.cache/ms-playwright 2>&1 | head -20 || true"])["stdout"].strip(),
    "numpy_version": run(["sh", "-c", "python3 -c 'import numpy,sys;print(numpy.__version__)' 2>&1"])["stdout"].strip(),
    "onnxruntime_version": run(["sh", "-c", "python3 -c 'import onnxruntime;print(onnxruntime.__version__)' 2>&1"])["stdout"].strip(),
    "rapidocr_version": run(["sh", "-c", "python3 -c 'import rapidocr;print(getattr(rapidocr,\"__version__\",\"?\"));print(rapidocr.__file__)' 2>&1"])["stdout"].strip(),
}

out["commands"] = [findr, dp, pyr, pipr]
print(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=False))
'@

$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($payload))
$auditT0 = [DateTimeOffset]::UtcNow
$audit = Invoke-Native -FilePath 'docker' -Arguments @(
    'run', '--rm', '-i', '--entrypoint', 'sh', $Image, '-c', 'base64 -d -i | python3 -'
) -StdIn $b64
$auditSeconds = [math]::Round(([DateTimeOffset]::UtcNow - $auditT0).TotalSeconds, 3)

if ($audit.ExitCode -ne 0) {
    Write-Host "----- container stderr -----" -ForegroundColor Red
    Write-Host $audit.StdErr
    Write-Error "in-container audit failed (exit $($audit.ExitCode))" -ErrorAction Continue
    exit 2
}
if ([string]::IsNullOrWhiteSpace($audit.StdOut)) {
    Write-Error "in-container audit produced no stdout" -ErrorAction Continue
    exit 2
}
try {
    $container = $audit.StdOut | ConvertFrom-Json -AsHashtable
}
catch {
    Write-Error "container payload returned invalid JSON: $($_.Exception.Message)" -ErrorAction Continue
    exit 3
}
Write-Step -Name 'container-audit' -ExitCode 0 -Detail ("$auditSeconds s, " + $audit.StdOut.Length + " bytes JSON")

# --------------------------------------------------------------------------- #
# 5. assemble + emit
# --------------------------------------------------------------------------- #
$scriptPath = $MyInvocation.MyCommand.Path
$scriptSha = if ($scriptPath -and (Test-Path $scriptPath)) {
    (Get-FileHash -Algorithm SHA256 -Path $scriptPath).Hash.ToLowerInvariant()
}
else { $null }

$doc = [ordered]@{
    schema      = 'pyknora.docreader_image_inventory/v1'
    task        = 'P0-T09R'
    generated_at = $script:StartedAt.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    host        = [ordered]@{
        os          = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
        powershell  = $PSVersionTable.PSVersion.ToString()
        docker      = $dockerVersion
        pwsh_path   = (Get-Process -Id $PID).Path
        script      = 'experiments/phase0/audit_docreader_image.ps1'
        script_sha256 = $scriptSha
    }
    pull        = $pull
    image       = $imageBlock
    registry    = $registryBlock
    container   = $container
    steps       = @($script:Steps)
    durations   = [ordered]@{ container_audit_seconds = $auditSeconds }
}

$json = $doc | ConvertTo-Json -Depth 40
if ($OutFile) {
    $resolved = [System.IO.Path]::GetFullPath($OutFile)
    [System.IO.File]::WriteAllText($resolved, $json, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[audit] wrote $resolved ($($json.Length) chars) sha256=$((Get-FileHash -Algorithm SHA256 -Path $resolved).Hash.ToLowerInvariant())" -ForegroundColor Green
}
else {
    $json
}
exit 0
