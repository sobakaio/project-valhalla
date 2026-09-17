# Valhalla Photo Studio

Valhalla Photo Studio 1.7.2 is a local production workspace for generating coherent Covered, Revealing, and Explicit photoshoots of adult women with your own ComfyUI instance. It turns creative direction into complete, automatically composed prompts, keeps the subject and visual story consistent across a set, and sends the approved shots to ComfyUI for image inference and Motion proofs.

The browser interface brings the full workflow together: storyboard planning, prompt automation, compatible scene and wardrobe selection, shot-level direction, Preview and Production rendering, progress tracking, and a built-in Proofs gallery for organizing and reviewing generated images and Motion outputs. Every rule-compatible storyboard is resolved before GPU work begins, so poses, wardrobe, scene geometry, camera direction, content progression, prompts, and seeds can be inspected or edited before an expensive render.

The application is designed for a private workstation or trusted LAN. It has no cloud service, account system, telemetry, or built-in authentication.

> **Adult-content notice:** the production catalog supports Covered, Revealing, and Explicit solo-adult modes. All configured subjects are adults aged 21–23. Use the application only where its content is lawful and appropriate.

## Version 1.7.2

This release adds a universal Settings workspace with separate Image and Video workflow and prompt-enhancer configuration, supports multiple enhancer models and instruction files, and refines the settings and capture workflows. It also keeps the Covered, Revealing, and Explicit content terminology consistent across the product.

## Highlights

- **Review before render:** resolve complete storyboards without GPU work, inspect every prompt and seed, then render only when the plan is ready.
- **Coherent photoshoots:** keep the subject, wardrobe, palette, location, mood, and visual treatment fixed while compatible poses and camera direction evolve.
- **Three content modes:** Covered, configurable Revealing content, and Explicit editorial arcs.
- **Director’s Desk:** edit compatible subject traits, wardrobe, stages, poses, actions, expressions, locations, surfaces, camera grammar, and explicit recipes at set or shot scope.
- **Deterministic production:** Storyboard seeds reproduce composition; separate image-variation seeds reproduce or vary rendered pixels.
- **Named ComfyUI profiles:** capture, validate, rename, and select independent Production and Preview workflows without manually editing workflow JSON.
- **Fast Preview:** render smaller temporary drafts while preserving the selected workflow’s base sampler, LoRA chain, CLIP path, and VAE.
- **Reliable job handling:** cancellable FIFO render queue, per-frame progress and ETA, reload-safe Logbook state, and clear first-error failure reporting.
- **Production gallery:** virtualized thumbnails, Photoshoots, Motion, and All proofs views, persistent thumbnail scaling (slider or Ctrl/Cmd + wheel), fullscreen inspection, zoom, slideshow, downloads, and confirmed deletion.
- **LTX 2.5 Motion:** turn any generated proof image into an audio-preserving image-to-video job with a manual motion prompt and independent Video workflow profile.
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

| All proofs | Fullscreen lightbox |
|---|---|
| [![Virtualized all-images gallery](screenshots/5-proofs-all-images.jpg)](screenshots/5-proofs-all-images.jpg) | [![Fullscreen image inspection lightbox](screenshots/6-proofs-lightbox.jpg)](screenshots/6-proofs-lightbox.jpg) |
| Move through the complete virtualized image and video collection. | Inspect full images and videos with fit, zoom, navigation, slideshow, download, and deletion controls. |

### Monitor and configure production

| Production Logbook | Settings |
|---|---|
| [![Production Logbook with progress, rendered image, and prompts](screenshots/7-logbook.jpg)](screenshots/7-logbook.jpg) | [![Settings with workflow profiles](screenshots/8-rendering-profiles.jpg)](screenshots/8-rendering-profiles.jpg) |
| Track progress, timing, outputs, prompts, seeds, and generation events. | Configure independent ComfyUI workflows and prompt enhancement for each media type. |

## Requirements

- Linux or another environment capable of running the included POSIX `launcher.sh`
- Python 3.11 or newer
- ComfyUI reachable locally or on a trusted LAN
- a modern browser
- a working ComfyUI image-generation workflow
- `ffmpeg` on `PATH` for Video gallery poster frames

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

3. Open **System → Settings** in Valhalla.
4. Capture the latest successful ComfyUI workflow, give it a clear profile name, and select it for Production and Preview.
5. Configure a batch in **Studio**, then choose **Resolve storyboard**.
6. Review the planned shots or refine them in **Director**.
7. Open the render dropdown in Studio or Director and optionally choose **Enhance prompts**. It processes only missing or changed image prompts and reports progress; rendering still computes an absent prompt on demand before GPU submission.
8. Choose **Preview storyboard** for drafts or **Render storyboard** for full production.

The launcher opens the Web UI automatically. It detects an existing server process belonging to this project and asks before stopping it. It never kills an unrelated Python process.

Direct startup is also available:

```bash
python3 server.py
python3 server.py --host 127.0.0.1 --port 9000
python3 server.py --no-browser
```

Stop the server with `Ctrl+C`. Closing the browser does not stop the server or an active render job.

## Settings

The Settings dialog manages named ComfyUI API workflows stored in `workflows/`. A profile is captured from the latest successful ComfyUI history entry and validated before it can be selected.

A usable profile must expose unambiguous nodes for:

- positive conditioning;
- seed control;
- the main sampler and latent input;
- VAE decoding;
- image output.

Negative conditioning is optional. If a workflow has no connected negative text encoder, Valhalla treats its positive prompt as the complete structural source of truth.

Image Production and Preview may use the same profile or different profiles. Preview reduces the detected latent dimensions to `comfy.preview_max_edge` while keeping orientation and approximate aspect ratio. It prunes downstream refiners/detailers but deliberately retains upstream LoRA nodes because they are part of the visual design.

Video profiles are independent from image profiles and are stored directly in
`workflows/` alongside image profiles. Profile filenames must therefore be
unique across both media types. A captured video workflow must expose a `LoadImage` input,
a text prompt, at least one scalar seed, a duration input, and a
`SaveVideo`, VHS `Video Combine`, or compatible video output node. The Studio
profile manager has separate Image and Video tabs; Video has a Production
profile only, while Image keeps its Production and Preview pair. Live mode is
also selected independently for each media type.

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

`skin_marking` is a weighted fixed subject trait with a dominant no-tan-lines state
(80%) plus bra-line, panty-line, and combined variants. Tan-line contrast adapts to
the selected light, medium, or dark skin tone. Bra tan lines compile only with
physically exposed chest
anatomy; panty tan lines compile only when the hips/pubic area is uncovered. The
selected marking remains stable across a Photoshoot and Covered prompts do not
mention hidden tan-line anatomy.

If `comfy.workflow_source` is set to `live`, Valhalla reads the latest compatible ComfyUI image workflow instead of the selected saved profiles. The
`comfy.media_profiles` object can select Image and Video sources independently;
existing configurations are migrated when a profile selection is saved. Saved
profiles are recommended for reproducible production.

## Production workflow

### Modes

- **Photoshoot** creates coherent sets with gradual, non-reversing Covered → Revealing → Explicit stages.
- **Random** rebuilds the subject context and scene for every frame while honoring the configured Revealing and Explicit shares.
- **Covered** server-enforces fully clothed, non-suggestive stages and removes incompatible garments, actions, poses, intensities, and imports.
- **Explicit** starts fully nude and plans a seeded editorial arc across concrete recipe, pose, action, camera, and intensity families, reserving a compatible peak closing frame.

The content bands are deliberately distinct: **Covered** means fully clothed with
no suggestive content or poses (revealing underwear is excluded); **Revealing**
means partially undressed, including underwear or lingerie, with suggestive
content or poses allowed; **Explicit** means fully nude with explicit poses or
actions.

### Content shares

In the Revealing content mode, the sliders apply to each set independently:

- **Revealing share** is the percentage of frames that go beyond Covered. It
  includes all Revealing and Explicit frames.
- **Explicit share** is the percentage of those Revealing-share frames that are
  Explicit. It is not a percentage of the whole storyboard: for 12 shots with
  a 50% Revealing share and 30% Explicit share, 6 shots are beyond Covered and
  2 of those are Explicit.

Photoshoot places these frames at the end of each coherent set. Random mixes
the same exact counts across independent frames. A zero Revealing share keeps
every frame Covered; a 100% Explicit share makes every Revealing-share frame
Explicit.

### Seeds

- **Storyboard seed** controls the complete compositional plan.
- **Image variation seed** changes rendered pixels without rebuilding direction.
- Variation can be fixed, fresh for every frame, or deterministically derived per photoshoot and shot.

Storyboard export stores the selected catalog IDs, workflow profile, prompts, and effective seeds. Imports are accepted only when their semantic database fingerprint matches the current catalog.

### Rendering and outputs

Production jobs are immutable snapshots placed in a shared FIFO queue. Image
jobs contain one output per shot; a Video job contains one LTX 2.5 image-to-video
render from a selected proof image. Cancellation takes effect between images,
and a queued video can be cancelled before it is submitted to ComfyUI.
Generated images and videos are written to `storage.output_dir`; each generated
image also gets a hidden `.image-name.json` sidecar containing only the final image
prompt needed by downstream video prompt generation. Temporary shot previews
remain in memory and are discarded when closed.

Rendering has two independent dimensions: generation mode (`Photoshoot` or `Random`)
and render tier (`Production` or `Preview`). Both tiers use the same grouping,
labels, queue behavior, and logs. Output names encode both dimensions, for example
`..._photoshoot_001_production_shot_001_...` and
`..._random_001_preview_shot_001_...`; older naming schemes are not parsed.

Queued and rendering frames appear immediately in Proofs as neutral placeholder
thumbnails. The server sends logical group ranges and gallery virtualization
materializes only the visible placeholder window, so large queues do not create
thousands of DOM cards. Inside a group cards read `Shot N` with `Production` or
`Preview`, followed by the active frame ETA or `Queued`. At gallery root they read
`Photoshoot N` or `Random N`, with the tier and the active logical-group ETA. The
job dock estimates the whole active job; later FIFO jobs remain honestly labeled
`Queued`. The queue can be paused from the job dock: the active render continues,
while queued image and video jobs wait until the queue is resumed.
Completed outputs replace their pending positions in place. Pending cards never enter
filesystem proof listings, request thumbnail bytes, expose prompt metadata, or enable
fullscreen/download/delete.

Output deletion is permanent and requires confirmation. Deletion is disabled while a render job is active. Restarting Valhalla clears in-memory planning and job history but never removes generated files.

### Creating videos

Open an image in the Proof Gallery and choose **Create video**. Enter optional
motion/camera guidance, then choose **Create prompt**. The generated LTX prompt
is editable in the second textarea; review it and choose **Queue video** with a
whole-number duration from 1 to 60 seconds. The
video uses the selected Video Production profile, is added to the same FIFO GPU
queue as image renders, and appears in the **Motion** gallery view when ready. While
the source lightbox remains open, the shared job dock is shown inside it immediately
after queueing and continues to report progress.
The prompt enhancer reads the selected image's hidden `.image-name.json` sidecar,
inserts the image prompt into `{{INPUT_DATA}}`, the first textarea into
`{{USER_GUIDANCE}}`, and the selected duration into `{{VIDEO_LENGTH}}` in
`instructions_video`. The final generated prompt is sent to LTX only after it is
reviewed in the second textarea.
ComfyUI video outputs are copied without re-encoding, so audio tracks returned
by the workflow remain embedded. Each generated video filename includes the
source proof media ID, for example `..._video_from_6909363532413516788_image_01_...`;
this keeps the source relationship filesystem-native across server restarts.

## Configuration

Runtime settings live in `config.json`. Relative paths are resolved from the project directory.

| Section | Important settings |
|---|---|
| `server` | listen host and port; keep loopback unless trusted-LAN access is required |
| `comfy` | ComfyUI URL, image/video workflow sources and profiles, timeouts, Preview size |
| `prompt_enhancer` | optional local OpenAI-compatible prompt rewrite with independent `production` and `preview` flags; `models` is an array of `{ "model": "..." }` entries, while `image_model` and `video_model` select independently from it; image instructions are read from `instructions_image`, video instructions are read from `instructions_video`, `parallel` limits concurrent batch requests, sampling uses `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, and `repeat_penalty`, and authentication uses `LLAMA_API_KEY` |
| `storage` | output directory, additional proof directories, PNG/JPEG output, JPEG quality, and optional age-free prompt/result JSONL debug log |
| `gallery` | thumbnail size and bounded in-memory thumbnail cache |
| `interface` | privacy auto-cover intervals |
| `limits` | scene retries and retained in-memory storyboards, jobs, and previews |

The **Settings → Prompt enhancer** tab lists the available Image and Video instruction files from `instructions/` and saves the selected paths back to `instructions_image` and `instructions_video`.

The repository includes safe starter templates at
`instructions/image-inference.md.example` and
`instructions/video-inference.md.example`. Copy either template to the matching
`.md` filename and customize it locally; active instruction files remain ignored
so private prompt policies are not committed.

Set `storage.prompt_debug_log.enabled` to `true` to append one compact JSONL
record after every successful render, saved shot preview, or in-memory
Shot Preview. Each record maps the output filename (or `preview:<id>`) to its seed,
workflow, positive prompt, and auxiliary conditioning. The logged positive prompt
uses `adult woman` in place of the configured exact age; rendering still receives
the original age prompt. Relative log paths are resolved from `config.json`.

Restart the server after editing `config.json`. The application has no authentication, so do not bind it to an untrusted network.

When `prompt_enhancer.production` or `prompt_enhancer.preview` is `true`, the
compiled image prompt for that render tier is sent to the configured local
`/v1/chat/completions` endpoint before being submitted to ComfyUI. The system
instruction is read fresh from `instructions_image` for each request and is not
duplicated in the application code. Set `LLAMA_API_KEY` before starting the
server when the local LLM endpoint requires authentication. This setting applies
only to image renders.

`prompt_enhancer.parallel` limits the number of concurrent requests during batch
prompt preparation. Set it to the number of parallel slots configured in the
local LLM server (for example, launch the server with `--parallel 4`). This is a
client-side concurrency limit and is not sent as a chat-completion parameter.

The **Enhance prompts** action is available in both Studio and Director
render dropdowns. It keeps optimized prompts in server memory for the current
session, prepares shots concurrently up to `prompt_enhancer.parallel`, skips
prompts whose compiled input and instructions are unchanged, and marks changed
shots for recomputation. If a
prompt is absent or stale when rendering starts, the normal LLM request runs
immediately before that shot is submitted to ComfyUI. Editing either active
instruction Markdown file takes effect on the next status check or request;
no server restart is needed for those Markdown edits.

Creative records and selection rules live in `database.json`, not `config.json`. Records can be temporarily removed from selection with `"disabled": true`.

## Validation and catalog statistics

Run the complete GPU-free production audit before a large render batch or after changing `database.json`:

```bash
./launcher.sh validate
# equivalent: python3 server.py validate
```

Validation checks configuration and catalog structure, exact record reachability, every outfit/interior combination, Covered contracts, garment transitions, representative storyboards in every mode, all explicit recipes, and 10,000 camera-grammar scenes. It never contacts ComfyUI or writes outputs. Proven failures return a non-zero exit code; finite-sample gaps are warnings only when the exact analyzer proves a valid route.

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
workflows/      captured image and video profiles
outputs/        default production output directory
launcher.sh     dependency check and application launcher
tests/          deterministic regression and stress tests
```

## Privacy and operational boundaries

- Everything runs locally unless `config.json` points to another trusted ComfyUI host.
- The privacy cover hides images and prompts in the UI; it is not encryption or access control.
- Server, storyboard, and current-session Logbook state is held in memory; generated media files remain on disk. The optional prompt debug log is a separate diagnostic record and is not used to restore Logbook state.
- Valhalla does not include an image-quality detector or identity/reference-image pipeline.
- ComfyUI errors and invalid workflows are reported before or during the affected job without silently skipping failed frames.
