package boot

import (
	"errors"
	"strings"
	"testing"

	"github.com/summon/agent/sdr"
)

func TestEnabledAPIKey(t *testing.T) {
	if got := enabledAPIKey(true, "secret-key"); got != "secret-key" {
		t.Fatalf("enabled API key = %q, want %q", got, "secret-key")
	}
	if got := enabledAPIKey(false, "secret-key"); got != "" {
		t.Fatalf("disabled API key = %q, want empty", got)
	}
}

func TestConfigNameValidationRejectsRCONInjection(t *testing.T) {
	valid := []string{"rgl_6s_5cp_match_pro", "etf2l_6v6_5cp"}
	for _, name := range valid {
		if !safeConfigNameRE.MatchString(name) {
			t.Errorf("expected config name %q to be valid", name)
		}
	}

	invalid := []string{"rgl_match; quit", "rgl_match\nquit", `rgl_"match`}
	for _, name := range invalid {
		if safeConfigNameRE.MatchString(name) {
			t.Errorf("expected config name %q to be invalid", name)
		}
	}
}

func TestDesiredMapAlreadyLoaded(t *testing.T) {
	statusFailure := errors.New("status unavailable")
	tests := []struct {
		name       string
		desiredMap string
		serverInfo *sdr.ServerInfo
		statusErr  error
		want       bool
	}{
		{
			name: "matching map", desiredMap: "cp_process_f12",
			serverInfo: &sdr.ServerInfo{Map: "cp_process_f12"}, want: true,
		},
		{
			name: "engine-normalized case", desiredMap: "CP_PROCESS_F12",
			serverInfo: &sdr.ServerInfo{Map: "cp_process_f12"}, want: true,
		},
		{
			name: "different map", desiredMap: "cp_process_f12",
			serverInfo: &sdr.ServerInfo{Map: "cp_badlands"}, want: false,
		},
		{
			name: "missing map in status", desiredMap: "cp_process_f12",
			serverInfo: &sdr.ServerInfo{}, want: false,
		},
		{
			name: "missing server info", desiredMap: "cp_process_f12",
			serverInfo: nil, want: false,
		},
		{
			name: "status error preserves fallback", desiredMap: "cp_process_f12",
			serverInfo: &sdr.ServerInfo{Map: "cp_process_f12"}, statusErr: statusFailure,
			want: false,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := desiredMapAlreadyLoaded(test.desiredMap, test.serverInfo, test.statusErr); got != test.want {
				t.Fatalf("desiredMapAlreadyLoaded() = %v, want %v", got, test.want)
			}
		})
	}
}

func TestMapNameValidationRejectsRCONInjection(t *testing.T) {
	valid := []string{"cp_process_f12", "koth_product_rcx"}
	for _, name := range valid {
		if !isSafeMapName(name) {
			t.Errorf("expected map name %q to be valid", name)
		}
	}

	invalid := []string{
		"cp_process; quit",
		"cp_process\nquit",
		"../cp_process",
		"workshop/cp_process",
		strings.Repeat("a", 65),
	}
	for _, name := range invalid {
		if isSafeMapName(name) {
			t.Errorf("expected map name %q to be invalid", name)
		}
	}
}
