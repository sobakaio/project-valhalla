# Valhalla Photo Studio

Valhalla Photo Studio 1.5 is a local production workspace for generating coherent SFW and NSFW photoshoots of adult women with your own ComfyUI instance. It turns creative direction into complete, automatically composed prompts, keeps the subject and visual story consistent across a set, and sends the approved shots to ComfyUI for image inference.

The browser interface brings the full workflow together: storyboard planning, prompt automation, compatible scene and wardrobe selection, shot-level direction, Preview and Production rendering, progress tracking, and a built-in gallery manager for organizing and reviewing the resulting images. Every rule-compatible storyboard is resolved before GPU work begins, so poses, wardrobe, scene geometry, camera direction, content progression, prompts, and seeds can be inspected or edited before an expensive render.

The application is designed for a private workstation or trusted LAN. It has no cloud service, account system, telemetry, or built-in authentication.

> **Adult-content notice:** the production catalog supports SFW, progressive adult, and explicit solo-adult modes. All configured subjects are adults aged 21–23. Use the application only where its content is lawful and appropriate.

## Highlights

- **Review before render:** resolve complete storyboards without GPU work, inspect every prompt and seed, then render only when the plan is ready.
- **Coherent photoshoots:** keep the subject, wardrobe, palette, location, mood, and visual treatment fixed while compatible poses and camera direction evolve.
- **Three content modes:** enforced SFW-only, configurable progressive content, and Full XXX editorial arcs.
- **Director’s Desk:** edit compatible subject traits, wardrobe, stages, poses, actions, expressions, locations, surfaces, camera grammar, and explicit recipes at set or shot scope.
- **Deterministic production:** Storyboard seeds reproduce composition; separate image-variation seeds reproduce or vary rendered pixels.
- **Named ComfyUI profiles:** capture, validate, rename, and select independent Production and Preview workflows without manually editing workflow JSON.
- **Fast Preview:** render smaller temporary drafts while preserving the selected workflow’s base sampler, LoRA chain, CLIP path, and VAE.
- **Reliable job handling:** cancellable FIFO render queue, per-frame progress and ETA, reload-safe Logbook state, and clear first-error failure reporting.
- **Production gallery:** virtualized thumbnails, photoshoot grouping, persistent thumbnail scaling (slider or Ctrl/Cmd + wheel), fullscreen inspection, zoom, slideshow, downloads, and confirmed deletion.
- **Privacy controls:** instantly hide decoded images and prompts with a configurable shortcut or inactivity timer; covering never deletes files or cancels renders.
- **Validated catalog:** more than 1,100 subject, wardrobe, location, direction, camera, and treatment records with exact reachability analysis.

## Interface tour

### Plan and direct

| Photo Studio | Director's Desk |
|---|---|
| [![Photo Studio with run setup and a resolved storyboard](screenshots/1-studio.jpg)](screenshots/1-studio.jpg) | [![Director's Desk with shot navigation and compatible controls](screenshots/2-director.jpg)](screenshots/2-director.jpg) |
| Resolve and review complete productions before committing GPU time. | Refine subject, wardrobe, scene, camera, and shot direction with compatible controls. |

### Review coherent photoshoots

| Photoshoot overview | One complete photoshoot |
|---|---|
| [![Proof Gallery grouped into coherent photoshoots](screenshots/3-proofs-photoshoots.jpg)](screenshots/3-proofs-photoshoots.jpg) | [![Proof Gallery showing all images from one photoshoot](screenshots/4-proofs-photoshoot.jpg)](screenshots/4-proofs-photoshoot.jpg) |
| Browse large productions as recognizable sets. | Open a set to inspect, download, or manage its individual shots. |

### Browse and inspect outputs

| All images | Fullscreen lightbox |
|---|---|
| [![Virtualized all-images gallery](screenshots/5-proofs-all-images.jpg)](screenshots/5-proofs-all-images.jpg) | [![Fullscreen image inspection lightbox](screenshots/6-proofs-lightbox.jpg)](screenshots/6-proofs-lightbox.jpg) |
| Move through the complete virtualized output collection. | Inspect full images with fit, zoom, navigation, slideshow, download, and deletion controls. |

### Monitor and configure production

| Production Logbook | Rendering profiles |
|---|---|
| [![Production Logbook with progress, rendered image, and prompts](screenshots/7-logbook.jpg)](screenshots/7-logbook.jpg) | [![ComfyUI rendering profile manager](screenshots/8-rendering-profiles.jpg)](screenshots/8-rendering-profiles.jpg) |
| Track progress, timing, outputs, prompts, seeds, and generation events. | Capture and select independent ComfyUI workflows for Production and Preview. |

## Requirements

- Linux or another environment capable of running the included POSIX `launcher.sh`
- Python 3.11 or newer
- ComfyUI reachable locally or on a trusted LAN
- a modern browser
- a working ComfyUI image-generation workflow

The launcher installs the two Python packages used at runtime if necessary:

- `requests`
- `Pillow`

ComfyUI defaults to `http://127.0.0.1:8188`. Valhalla defaults to port `8765` and may be restricted to loopback or exposed to a trusted LAN through `config.json`.

## Quick start

1. Start ComfyUI and successfully run the workflow you want Valhalla to control.
2. From the project directory, start Valhalla:

   ```bash
   ./launcher.sh
   ```

3. Open **System → Rendering profiles** in Valhalla.
4. Capture the latest successful ComfyUI workflow, give it a clear profile name, and select it for Production and Preview.
5. Configure a batch in **Studio**, then choose **Resolve storyboard**.
6. Review the planned shots or refine them in **Director**.
7. Choose **Preview storyboard** for drafts or **Render storyboard** for full production.

The launcher opens the Web UI automatically. It detects an existing server process belonging to this project and asks before stopping it. It never kills an unrelated Python process.

Direct startup is also available:

```bash
python3 server.py
python3 server.py --host 127.0.0.1 --port 9000
python3 server.py --no-browser
```

Stop the server with `Ctrl+C`. Closing the browser does not stop the server or an active render job.

## Rendering profiles

Valhalla renders through named ComfyUI API workflows stored in `workflows/`. A profile is captured from the latest successful ComfyUI history entry and validated before it can be selected.

A usable profile must expose unambiguous nodes for:

- positive conditioning;
- seed control;
- the main sampler and latent input;
- VAE decoding;
- image output.

Negative conditioning is optional. If a workflow has no connected negative text encoder, Valhalla treats its positive prompt as the complete structural source of truth.

Production and Preview may use the same profile or different profiles. Preview reduces the detected latent dimensions to `comfy.preview_max_edge` while keeping orientation and approximate aspect ratio. It prunes downstream refiners/detailers but deliberately retains upstream LoRA nodes because they are part of the visual design.

`database.json` may define reusable `settings.workflow_lora_rules`. A rule targets
the exact configurable `lora_name`, matches resolved shot semantics such as stage,
body visibility, visible garment slots, or garment tags, and overrides scalar
`strength_model` and/or `strength_clip` on the per-shot workflow copy. The
matching rules are applied FIFO in their database declaration order; later rules
may refine values written by earlier ones. Missing LoRAs, nodes, or scalar inputs
are always ignored, while unmatched shots preserve the captured profile strength.
The supplied anatomy LoRA rule lowers model strength while genitals are covered
and leaves the captured strength unchanged when they are exposed.

Hosiery garments declare a structural `support_mode`: `waist_continuous`,
`self_supporting`, or `garter_required`. The compiler emits a distinct physical
construction contract for each mode. Garter-supported stockings can only be
composed with the dedicated visible support belt; ordinary pantyhose and stay-up
stockings cannot acquire that belt through Composer or Director edits.

Within a Photoshoot, surface color and texture are cached by interior and physical
furniture family. Alternate sofa catalog records therefore retain one sofa style,
while a pose may still move to a genuinely different support such as a bed, wall,
or floor. Random generations receive a fresh context. Teddy-bear props use authored
brown, cream, muted pink, and soft gray variants instead of an unspecified color.

Pose, action, and prop compatibility shares one two-hand budget. Explicit
`hands_required` values override a conservative wording-based fallback for legacy
catalog records, so one-hand gestures cannot be paired with a pose already using
both hands. Finger-to-mouth and middle-finger gestures are available across the
authored covered-to-explicit range; intimate spreading explicitly uses one hand's
index and middle fingers and requires uncovered anatomy plus open-leg geometry.

Garments may declare an ordered `supported_states` sequence. The first structural
slice covers button-front shirts (`worn_closed` → `unbuttoned_open` → `removed`)
and jeans (`worn_closed` → `lowered_to_hips` → `removed`). The intermediate state
is compiled while the garment remains physically visible, with the underlying bra
or panties exposed as appropriate; a later removal shot only describes full removal.

`skin_marking` is a weighted fixed subject trait with even, bra-line, panty-line,
and combined variants. Bra tan lines compile only with physically exposed chest
anatomy; panty tan lines compile only when the hips/pubic area is uncovered. The
selected marking remains stable across a Photoshoot and covered/SFW prompts do not
mention hidden tan-line anatomy.

If `comfy.workflow_source` is set to `live`, Valhalla reads the latest compatible ComfyUI workflow instead of the selected saved profiles. Saved profiles are recommended for reproducible production.

## Production workflow

### Modes

- **Photoshoot** creates coherent sets with progressive, non-reversing garment and content stages.
- **Random** rebuilds the subject context and scene for every frame.
- **SFW only** server-enforces fully covered stages and removes incompatible garments, actions, poses, intensities, and imports.
- **Full XXX** starts explicitly and plans a seeded editorial arc across concrete recipe, pose, action, camera, and intensity families, reserving a compatible peak closing frame.

### Seeds

- **Storyboard seed** controls the complete compositional plan.
- **Image variation seed** changes rendered pixels without rebuilding direction.
- Variation can be fixed, fresh for every frame, or deterministically derived per photoshoot and shot.

Storyboard export stores the selected catalog IDs, workflow profile, prompts, and effective seeds. Imports are accepted only when their semantic database fingerprint matches the current catalog.

### Rendering and outputs

Production jobs are immutable snapshots placed in a FIFO queue. Cancellation takes effect between images. Generated images are written to `storage.output_dir`; temporary shot previews remain in memory and are discarded when closed.

Queued and rendering Production/persistent Preview frames appear immediately in
Proofs as neutral placeholder thumbnails. The server sends one compact range per
job instead of thousands of frame records, and gallery virtualization materializes
only the visible placeholder window. Queued cards show only their render kind and
shot number; the currently rendering frame alone receives an opportunistic local ETA.
Completed outputs replace their pending positions in place. Pending cards never enter
filesystem proof listings, request thumbnail bytes, expose prompt metadata, or enable
fullscreen/download/delete.

Output deletion is permanent and requires confirmation. Deletion is disabled while a render job is active. Restarting Valhalla clears in-memory planning and job history but never removes generated files.

## Configuration

Runtime settings live in `config.json`. Relative paths are resolved from the project directory.

| Section | Important settings |
|---|---|
| `server` | listen host and port; keep loopback unless trusted-LAN access is required |
| `comfy` | ComfyUI URL, workflow source, profiles directory, Production/Preview selections, timeouts, Preview size |
| `storage` | output directory, additional proof directories, PNG/JPEG output, JPEG quality, EXIF stripping, optional age-free prompt/result JSONL debug log |
| `gallery` | thumbnail size and bounded in-memory thumbnail cache |
| `interface` | privacy auto-cover intervals |
| `limits` | scene retries and retained in-memory storyboards, jobs, and previews |

Set `storage.prompt_debug_log.enabled` to `true` to append one compact JSONL
record after every successful Production image, saved Preview image, or in-memory
Shot Preview. Each record maps the output filename (or `preview:<id>`) to its seed,
workflow, positive prompt, and auxiliary conditioning. The logged positive prompt
uses `adult woman` in place of the configured exact age; rendering still receives
the original age prompt. Relative log paths are resolved from `config.json`.

Restart the server after editing `config.json`. The application has no authentication, so do not bind it to an untrusted network.

Creative records and selection rules live in `database.json`, not `config.json`. Records can be temporarily removed from selection with `"disabled": true`.

## Validation and catalog statistics

Run the complete GPU-free production audit before a large render batch or after changing `database.json`:

```bash
./launcher.sh validate
# equivalent: python3 server.py validate
```

Validation checks configuration and catalog structure, exact record reachability, every outfit/interior combination, SFW contracts, garment transitions, representative storyboards in every mode, all explicit recipes, and 10,000 camera-grammar scenes. It never contacts ComfyUI or writes outputs. Proven failures return a non-zero exit code; finite-sample gaps are warnings only when the exact analyzer proves a valid route.

Inspect diversity and narrow candidate pools with:

```bash
./launcher.sh stats
# equivalent: python3 server.py stats
```

Statistics include record and tag counts, manual versus automatic reachability, recipe coverage, and minimum/median/maximum garment-slot and furniture pools.

Run the regression suite with:

```bash
PYTHONPATH=.:tests python3 -m unittest discover -s tests
```

## Project layout

```text
server.py       composition engine, ComfyUI client, HTTP server, validation CLI
client/         browser interface
database.json   creative catalog and compatibility rules
config.json     local runtime configuration
workflows/      captured ComfyUI rendering profiles
outputs/        default production output directory
launcher.sh     dependency check and application launcher
tests/          deterministic regression and stress tests
```

## Privacy and operational boundaries

- Everything runs locally unless `config.json` points to another trusted ComfyUI host.
- The privacy cover hides images and prompts in the UI; it is not encryption or access control.
- Server and storyboard state is held in memory; generated image files remain on disk.
- Valhalla does not include an image-quality detector or identity/reference-image pipeline.
- ComfyUI errors and invalid workflows are reported before or during the affected job without silently skipping failed frames.
