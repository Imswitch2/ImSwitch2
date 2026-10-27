# Bridge jars for the Fiji host

PyImageJ refuses to start unless `imglib2-imglyb` is on the classpath. Left to
itself it resolves that jar from Maven through jgo, which pulls 38 jars,
including imglib2 5.12.0 alongside the imglib2 8.0.0 in Fiji Latest. That is a
version skew inside one JVM, and the first start needs the network. The host
therefore clears scyjava's endpoints and puts exactly these two jars on the
classpath (verified 2026-10-26: 7 s offline start against Fiji Latest arm64).

| Jar | Source | SHA-256 | License |
|---|---|---|---|
| `imglib2-imglyb-1.1.0.jar` | maven.scijava.org `net.imglib2:imglib2-imglyb:1.1.0` (SHA-1 `7cd2f0b7b24575f16e302fc0b0e27af4d3018b6a` matched the repository's `.sha1`) | `010e547ba4d3d4dc6d1c2cf94247ca8c6190344e1a829435e4ff48c36508e789` | Simplified BSD |
| `imglib2-unsafe-1.0.0.jar` | Maven Central `net.imglib2:imglib2-unsafe:1.0.0` (SHA-1 `6896d70ad30b249f2c720f3bac48baeaf8e070d1` matched) | `2077441d94f7198545e9d939e89a0358d9587bdf5c517a156fb8efe09349f62b` | Simplified BSD |

`imglib2-imglyb 1.1.0` is the version the Python package `imglyb` 2.1.0
requests: importing it appends `net.imglib2:imglib2-imglyb:1.1.0` to
`scyjava.config.endpoints`. The host compares that request with the version
here and refuses to start on a mismatch, rather than mixing versions.

To update: download the new versions from the same repositories, check the
SHA-1 against the repository's `.sha1` file, and update this table and the
host's version check together.
