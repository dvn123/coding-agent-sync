package unwrap

import (
	"slices"
	"strings"
)

// uv run's options that take the next word (`uv run --help`, uv 0.9). Any
// other option is a flag.
var uvRunValues = []string{
	"--extra", "--no-extra", "--group", "--no-group", "--only-group", "--no-editable-package", "--env-file", "-w",
	"--with", "--with-editable", "--with-requirements", "--package", "--python-platform", "--index",
	"--default-index", "-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--index-strategy",
	"--keyring-provider", "-P", "--upgrade-package", "--upgrade-group", "--resolution", "--prerelease",
	"--prerelease-package", "--fork-strategy", "--exclude-newer", "--exclude-newer-package", "--no-sources-package",
	"--reinstall-package", "--link-mode", "-C", "--config-setting", "--config-settings-package",
	"--no-build-isolation-package", "--no-build-package", "--no-binary-package", "--cache-dir", "--refresh-package",
	"-p", "--python", "--color", "--allow-insecure-host", "--directory", "--project", "--config-file",
}

// uvRunArguments is a `uv run` command's words with the run-time parts after
// its literal target marked Opaque, and that target: uv starts the target
// with every later word as its argument, so none of them changes what runs.
// ok is false for any other command, or when the target is not literal.
func uvRunArguments(values []string) ([]string, string, bool) {
	if len(values) < 3 || Basename(values[0]) != "uv" || values[1] != "run" {
		return nil, "", false
	}
	out := slices.Clone(values)
	for index := 2; index < len(out); index++ {
		arg := out[index]
		switch {
		case runTime(arg):
			return nil, "", false
		case arg == "--":
			continue
		case strings.HasPrefix(arg, "-"):
			if slices.Contains(uvRunValues, arg) {
				index++
			}
			continue
		}
		for after := index + 1; after < len(out); after++ {
			for _, mark := range []string{Wild, OneWord} {
				out[after] = strings.ReplaceAll(out[after], mark, Opaque)
			}
		}
		return out, arg, true
	}
	return nil, "", false
}
