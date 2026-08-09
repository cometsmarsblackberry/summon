package boot

import "testing"

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
