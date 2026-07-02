# ImProcess Icon Selection Sheet

Use this sheet to choose final icons for the always-present ImProcess toolbar
and Analyze menu actions.

## How to choose

1. Refresh/install the environment so `qtawesome` is available.
2. Run `qta-browser`.
3. Prefer Material Design Icons 6 (`mdi6.<icon-name>`) where it fits, but use
   another QtAwesome family when the metaphor is clearer.
4. Paste your choice into the `Selected icon` column.
5. Leave `Selected icon` blank to keep the current icon.

Valid QtAwesome prefixes include `mdi.*`, `mdi6.*`, `fa5s.*`, `fa6s.*`,
`ph.*`, `ri.*` and `codicon.*`.

## File Toolbar

| Action ID | UI label | Meaning | Current icon | Selected icon | Notes |
| --- | --- | --- | --- | --- | --- |
| `quick-load-data` | Quick load data... | Open the quick data loader. | `fa5s.file-upload` |  |  |
| `save-reconstruction` | Save reconstruction... | Save the active reconstruction. | `fa5s.file-download` |  |  |

## Image Toolbar

| Action ID | UI label | Meaning | Current icon               | Selected icon | Notes |
| --- | --- | --- |----------------------------| --- | --- |
| `auto-contrast` | Auto contrast | Automatically stretch display levels for the active image. | `mdi.contrast-circle`      |  |  |
| `brightness-contrast` | Brightness/Contrast... | Open the min/max brightness and contrast dialog. | `mdi.contrast`             |  |  |
| `reset-contrast` | Reset contrast | Reset display levels to the finite data range. | `mdi.contrast-box`         |  |  |
| `channels` | Channels... | Show per-layer visibility and LUT controls. | `mdi6.layers-triple`       |  |  |
| `duplicate` | Duplicate | Duplicate the active result. | `mdi6.layers-plus`   |  |  |
| `crop-substack` | Crop/Substack... | Create a cropped or ranged substack. | `mdi6.layers-minus`                |  |  |
| `max-projection` | Max projection | Collapse the default stack axis by maximum intensity. | `fa6s.arrows-down-to-line` |  |  |
| `split-stack` | Split stack | Split a stack into one result per plane. | `mdi.arrow-split-horizontal`          |  |  |
| `split-channels` | Split channels | Split a channel-like axis into one result per channel. | `mdi6.arrow-split-vertical` |  |  |
| `merge-channels` | Merge channels | Merge selected grayscale results into a channel stack. | `mdi.arrow-collapse-vertical`          |  |  |
| `make-composite` | Make composite | Render channel-like axes as colored display layers. | `mdi6.layers-edit`              |  |  |
| `make-rgb` | Make RGB | Bake channel-like axes into an RGB visualization result. | `mdi6.google-circles-communities`              |  |  |
| `reset-view` | Reset view | Reset the reconstruction viewer camera. | `ph.arrows-out-thin`       |  |  |

## Analyze Panel Shortcuts

| Action ID | UI label | Meaning | Current icon | Selected icon | Notes |
| --- | --- | --- | --- | --- | --- |
| `roi-manager` | ROI manager | Open the ROI manager panel. | `mdi6.select-marker` |  |  |
| `projection` | Projection | Open the projection panel. | `mdi6.axis-arrow` |  |  |
| `segmentation` | Segmentation | Open the segmentation panel. | `ph.user-rectangle-fill` |  |  |
| `results-table` | Results | Open the results table panel. | `fa6s.rectangle-list` |  | QtAwesome uses `fa6s` for Font Awesome 6 solid icons. |
