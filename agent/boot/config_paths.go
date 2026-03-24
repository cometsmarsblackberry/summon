package boot

import (
	"os"
	"path/filepath"
)

const legacyConfigPath = "/etc/tf2-reservation.json"

// ConfigFilePaths returns possible locations for the persisted reservation config.
// Newer agents persist under the running user's config directory; older deployments
// may have a root-written file in /etc.
func ConfigFilePaths() []string {
	paths := make([]string, 0, 2)

	if cfgDir, err := os.UserConfigDir(); err == nil && cfgDir != "" {
		paths = append(paths, filepath.Join(cfgDir, "summon", "tf2-reservation.json"))
	}

	paths = append(paths, legacyConfigPath)

	// De-dup while preserving order.
	seen := map[string]struct{}{}
	out := make([]string, 0, len(paths))
	for _, p := range paths {
		if p == "" {
			continue
		}
		if _, ok := seen[p]; ok {
			continue
		}
		seen[p] = struct{}{}
		out = append(out, p)
	}
	return out
}
