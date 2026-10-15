package host

import (
	"encoding/json"
	"testing"

	"github.com/summon/agent/boot"
)

func TestMergeRestartConfigAppliesRotatedSecretsAndMOTDURL(t *testing.T) {
	config := boot.ReservationConfig{
		Password:     "old-password",
		RCONPassword: "old-rcon",
		TVPassword:   "old-tv",
		MOTDURL:      "https://summon.example/motd/old-token",
		ConfigFile:   "etf2l_6v6_5cp",
	}
	raw := json.RawMessage(`{
		"password": "new-password",
		"rcon_password": "new-rcon",
		"tv_password": "new-tv",
		"motd_url": "https://summon.example/motd/new-token"
	}`)

	if err := mergeRestartConfig(&config, raw); err != nil {
		t.Fatalf("mergeRestartConfig: %v", err)
	}

	if config.Password != "new-password" || config.RCONPassword != "new-rcon" || config.TVPassword != "new-tv" {
		t.Fatalf("passwords were not rotated: %+v", config)
	}
	if config.MOTDURL != "https://summon.example/motd/new-token" {
		t.Fatalf("MOTDURL = %q, want the rotated MOTD link", config.MOTDURL)
	}
	if config.ConfigFile != "etf2l_6v6_5cp" {
		t.Fatalf("ConfigFile = %q, fields absent from the update must be kept", config.ConfigFile)
	}
}
