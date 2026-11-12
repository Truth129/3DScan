# MCP server

`scripts/scan3d_mcp.py` lets an MCP client, such as Claude Code or Claude Desktop, work a running
3DScan. It calls the [HTTP API](API.md) like the browser does, so sign-in, access rules and the job
queue apply to it in the same way. It runs on its own, from any machine that can reach 3DScan.

**Patient data.** Everything a tool returns goes to the client's model provider. That includes case
names, series descriptions, measurements and slice images. Point it only at a 3DScan whose cases you
may share that way, such as a scratch server with the demo case.

## Run it

It needs only [uv](https://docs.astral.sh/uv/getting-started/installation/): no clone of 3DScan and
no `scripts/bootstrap.sh`. uv reads the script's own dependencies (the MCP SDK, httpx and NumPy),
keeps them apart from anything else, and downloads Python 3.12 or newer if the machine has none.
It has been tested on Linux only.

Run it straight from a release, using the tag of the 3DScan version the server runs:

```bash
claude mcp add scan3d -- uv run https://raw.githubusercontent.com/Truth129/3DScan/v0.3.0/scripts/scan3d_mcp.py --url https://3dscan.example
```

A tag never changes, so neither does the code this runs. To read the script first, download it and
run `uv run --script scan3d_mcp.py` with the same arguments. In a clone, `scripts/bootstrap.sh`
installs uv as `.tools/bin/uv`, and the script is `scripts/scan3d_mcp.py`.

Other clients take the same command and arguments in their MCP server settings. The server talks
over stdio.

| Setting                                      | Meaning                                                       |
| -------------------------------------------- | ------------------------------------------------------------- |
| `--url` or `SCAN3D_MCP_URL`                  | 3DScan's address. Required                                    |
| `SCAN3D_MCP_USERNAME`, `SCAN3D_MCP_PASSWORD` | The account to sign in with, when sign-in is on               |
| `SCAN3D_MCP_PASSWORD_FILE`                   | A file holding the password, instead of `SCAN3D_MCP_PASSWORD` |

With sign-in on, the address must be HTTPS or this machine, as for the browser. The tools can do
only what that account can.

## Tools

| Area         | Tools                                                                                                                                                  |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Library      | `status`, `list_cases`, `get_case`, `create_demo_case`, `import_folder`                                                                                |
| Segmentation | `segmentation_options`, `prepare_series`, `run_segmentation`, `list_jobs`, `wait_for_job`, `cancel_job`                                                |
| Revisions    | `list_revisions`, `get_revision`, `activate_revision`, `set_review_status`, `agreement`, `probe`                                                       |
| Viewing      | `view_slice_image`: one axial, coronal or sagittal slice as a PNG, with masks drawn on it                                                              |
| Coronary     | `run_coronary_analysis`, `list_coronary_analyses`, `get_coronary_analysis`: CT-FFR, for research use only                                              |
| Corrections  | `start_correction`, `start_new_segment`, `get_correction`, `add_marks`, `remove_last_marks`, `run_correction`, `save_correction`, `discard_correction` |
| Export       | `export_seg_file`: a DICOM SEG file written on the machine running the MCP server                                                                      |
| Anything     | `api_get`: any JSON `GET` route                                                                                                                        |

- Long jobs return while still running. `wait_for_job` waits for them, except a SEG export: call
  `export_seg_file` again with the same arguments to write its file.
- `view_slice_image` lists each image edge's patient direction, the area of each drawn mask and the
  image's `pixel_to_ras`.
- Points can be given in the pixels of a `view_slice_image` picture or as patient RAS millimetres.
  Strokes, outlines and boxes are given in a picture's pixels and stay on its plane, which must be
  one slice of the scan's own grid. On an oblique or tilted series only point marks work.
- A write reads the draft or case as it stands. To stop if someone else has changed it since, pass
  `expected_generation` or `expected_active_revision_id` from the result you acted on.

## Not covered

- Pictures of the 3D view, and so **PNG** and **DICOM**. The browser draws those.
- Scissors cuts, which need a 3D camera.
- Delete forever, Empty Trash, People, the TotalSegmentator licence and storage. These stay in
  the app.
- Uploading files from the client's machine, Orthanc and DICOMweb imports, and archive sends.
  `import_folder` imports from a folder on the 3DScan server, and `api_get` reads the archive routes.
