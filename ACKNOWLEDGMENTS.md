# Acknowledgments & third-party code

Imswitch2 stands on a lot of other people's work. This file records that in
three tiers:

1. **[Vendored & ported code](#1-vendored--ported-code)** — code from other
   projects that lives in this repository, with its licence.
2. **[Role-model projects](#2-role-model-projects)** — projects we learned
   from, matched the behaviour of, or took ideas from, without copying code.
3. **[Scientific citation](#3-scientific-citation)** — the papers behind the
   methods we implement.

If you think something is missing or wrong here, please tell us — see
[Corrections & contact](#corrections--contact). We would much rather add an
acknowledgment than keep an unattributed line of code.

---

## 1. Vendored & ported code

Code in this repository that originates from another project. Each item is
also marked with a `Provenance` block in the source file itself. Full licence
texts live in [`licenses/`](licenses/).

| Component | Upstream | Licence | Where it lives |
|---|---|---|---|
| Fast region properties | [fast-regionprops](https://github.com/maweigert/fast-regionprops) — Martin Weigert | BSD-3-Clause ([text](licenses/fast-regionprops-BSD-3-Clause.txt)) | `imswitch/imcommon/algorithms/fast_regionprops.py` — core vendored verbatim (upstream commit `89c6741`); the `ImSwitch2 extensions` section below the divider is ours |
| ROI/plot GUI tools | [Tormenta](https://github.com/fedebarabas/tormenta) — Federico Barabas | GPL-3.0 (same as this project; notices inline) | `imswitch/imcommon/view/guitools/pyqtgraphtools.py`, `imagetools.py` |
| Pyro5 serialisation helpers | Talley Lambert | BSD-3-Clause (notice inline in file) | `imswitch/imcontrol/controller/server/_serialize.py` |
| Contrast auto-scaling | ImageJ 1.x `ContrastEnhancer` routine | Public domain | `imswitch/imcommon/view/guitools/imagetools.py` |
| PI GCS device library | `PIPython` — Physik Instrumente | Vendor SDK — see upstream terms | `imswitch/imcontrol/model/interfaces/pipython/` |
| The Imaging Source wrapper | `pyicic` / IC Imaging Control | Vendor SDK — see upstream terms | `imswitch/imcontrol/model/interfaces/pyicic/` |
| Cobolt 06-01 laser driver | Derived from the Lantz Cobolt driver | See upstream terms | `imswitch/imcontrol/model/lantzdrivers/cobolt/` |

**Upstream project.** Imswitch2 is a fork of
[ImSwitch](https://github.com/ImSwitch/ImSwitch) (GPL-3.0), itself descended
from Tormenta. The whole repository inherits that lineage, not just the files
listed above.

### Known gaps

These are tracked openly rather than quietly:

- **fast-regionprops** declares `BSD-3-Clause` in its `pyproject.toml` but
  ships **no LICENSE file**, so there is no upstream copyright line to
  reproduce. We reproduce the standard BSD-3-Clause text naming the author
  from the project metadata. If the author publishes a LICENSE file, we will
  match it exactly.
- **Vendor SDKs** (`pipython`, `pyicic`, Cobolt/Lantz) predate this fork and
  their exact redistribution terms have not been re-verified by us. If you
  are a rights holder and redistribution here is not covered, contact us and
  we will vendor differently or drop to an optional dependency.

---

## 2. Role-model projects

No code copied — but these shaped how Imswitch2 works, and deserve credit.

- **[Fiji / ImageJ](https://fiji.sc/)** — the reference for scientific image
  processing UX. ImProcess deliberately mirrors its vocabulary and muscle
  memory: the Image / Image operations menu split, and operations like Image
  Calculator, Math, Filters, Transform, Scale, Type conversion, Subtract
  Background and binary/label morphology. Our keyboard defaults follow Fiji's
  where an equivalent exists (`Ctrl+O` open, `Ctrl+Shift+C` brightness/
  contrast, `Ctrl+H` histogram, `Ctrl+M` measure, ...). All of it is
  reimplemented on numpy/scipy/scikit-image — what we took is *behaviour*,
  which is exactly what makes a tool feel familiar.
- **[Picasso](https://github.com/jungmannlab/picasso)** (Jungmann Lab, MIT) —
  the model for our whole SMLM pipeline. Our spot detection
  (`smlm/detection.py`) and single-spot fitting (`smlm/fitting.py`) are
  independent implementations of the Picasso approach — the net-gradient
  detector, and the localize → filter → render flow — written against
  numpy/scipy from the published method (Schnitzbauer et al. 2017), not
  copied from Picasso source. Picasso's drop-in plugin ergonomics also
  inspired ImProcess's `.py` plugin folder. We reproduce their licence in
  [`licenses/picasso-MIT.txt`](licenses/picasso-MIT.txt) even though no code
  is vendored, because our implementation follows their method closely.
  **If any of it is closer to Picasso's code than we believe, tell us** — see
  [Corrections & contact](#corrections--contact).
- **[napari](https://napari.org/)** — the viewer Imswitch2 embeds, and the
  source of our layer/display-model thinking.

---

## 3. Scientific citation

If you use these parts of Imswitch2 in published work, please cite the
original methods — this matters more to the authors than a mention here.

- **ImSwitch** — Casas Moreno et al., *JOSS* 6(64):3394 (2021).
  [doi:10.21105/joss.03394](https://doi.org/10.21105/joss.03394) — see
  [`CITATION.cff`](CITATION.cff).
- **Picasso / SMLM localization** — Schnitzbauer et al., *Nature Protocols*
  12, 1198–1228 (2017). [doi:10.1038/nprot.2017.024](https://doi.org/10.1038/nprot.2017.024)
- **MoNaLISA** — Masullo et al., *Nature Communications* 9, 3281 (2018).
  [doi:10.1038/s41467-018-05799-w](https://doi.org/10.1038/s41467-018-05799-w)
- **Fiji** — Schindelin et al., *Nature Methods* 9, 676–682 (2012).
  [doi:10.1038/nmeth.2019](https://doi.org/10.1038/nmeth.2019)
- **scikit-image** — van der Walt et al., *PeerJ* 2:e453 (2014).
  [doi:10.7717/peerj.453](https://doi.org/10.7717/peerj.453)
- **napari** — napari contributors.
  [doi:10.5281/zenodo.3555620](https://doi.org/10.5281/zenodo.3555620)

---

## A note on AI-assisted development

Parts of Imswitch2 were written with AI assistance. Standard image-processing
and instrument-control routines have a limited number of sensible
implementations, so some of our code may read similarly to code in other
open-source projects even where it was written independently and no copying
took place. Where we knowingly used someone's code, it is listed in tier 1
with its licence; where we knowingly followed someone's design, it is listed
in tier 2.

We cannot rule out that an unattributed resemblance slipped through. We would
rather hear about it and fix it than leave it.

---

## Corrections & contact

**If you believe your project is the source of code or a design here and is
not credited — or is credited incorrectly — please
[open an issue](https://github.com/Imswitch2/Imswitch2/issues) or contact the
maintainers.** We commit to:

1. adding the attribution, and
2. reproducing your licence, relicensing, or **removing the code entirely** if
   there is a genuine licence conflict — whichever you need.

No argument, no delay. We are a research project and getting this right
matters more to us than any individual function.

*This is a good-faith statement of intent, not a legal disclaimer or a
substitute for licence compliance. Where we knowingly vendor code, we comply
with its licence — that is what tier 1 and [`licenses/`](licenses/) are for.*
