// Package host implements the persistent multi-slot Instant host runtime.
package host

import (
	"encoding/json"

	"github.com/summon/agent/boot"
)

const (
	// ProtocolVersion is the host protocol emitted by this agent release.
	ProtocolVersion = 1
	ProtocolMin     = 1
	ProtocolMax     = 1
)

// SlotDefinition is the immutable public port allocation configured by Summon.
type SlotDefinition struct {
	SlotID    int `json:"slot_id"`
	SlotIndex int `json:"slot_index"`
	GamePort  int `json:"game_port"`
	TVPort    int `json:"tv_port"`
}

// CommandEnvelope is shared by every assignment-scoped backend command.
type CommandEnvelope struct {
	Type           string          `json:"type"`
	Protocol       int             `json:"protocol"`
	CommandID      string          `json:"command_id"`
	ReservationID  int             `json:"reservation_id"`
	AssignmentID   int             `json:"assignment_id"`
	SlotID         int             `json:"slot_id"`
	SlotIndex      int             `json:"slot_index"`
	Generation     int             `json:"generation"`
	LeaseExpiresAt int64           `json:"lease_expires_at"`
	GamePort       int             `json:"game_port"`
	TVPort         int             `json:"tv_port"`
	ImageDigest    string          `json:"image_digest"`
	Config         json.RawMessage `json:"config"`
	Command        string          `json:"command"`
	LogsTF         bool            `json:"logs_tf"`
	DemosTF        bool            `json:"demos_tf"`
	Reason         string          `json:"reason"`
}

type hostConfiguration struct {
	Type                       string           `json:"type"`
	Protocol                   int              `json:"protocol"`
	HostID                     int              `json:"host_id"`
	HeartbeatIntervalSeconds   int              `json:"heartbeat_interval_seconds"`
	DesiredImage               string           `json:"desired_image"`
	ForceImagePrepare          bool             `json:"force_image_prepare"`
	VersionPin                 string           `json:"version_pin"`
	AgentManifestURL           string           `json:"agent_manifest_url"`
	UpdateCheckIntervalSeconds int              `json:"update_check_interval_seconds"`
	Slots                      []SlotDefinition `json:"slots"`
}

type imagePrepareCommand struct {
	Type      string `json:"type"`
	CommandID string `json:"command_id"`
	HostID    int    `json:"host_id"`
	Image     string `json:"image"`
}

type updateCommand struct {
	Type        string `json:"type"`
	CommandID   string `json:"command_id"`
	HostID      int    `json:"host_id"`
	ManifestURL string `json:"manifest_url"`
	VersionPin  string `json:"version_pin"`
}

type restartConfig struct {
	Password            *string `json:"password"`
	RCONPassword        *string `json:"rcon_password"`
	TVPassword          *string `json:"tv_password"`
	ConfigFile          *string `json:"config_file"`
	EnableLogsTFUpload  *bool   `json:"enable_logs_tf_upload"`
	EnableDemosTFUpload *bool   `json:"enable_demos_tf_upload"`
}

func mergeRestartConfig(config *boot.ReservationConfig, raw json.RawMessage) error {
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var update restartConfig
	if err := json.Unmarshal(raw, &update); err != nil {
		return err
	}
	if update.Password != nil {
		config.Password = *update.Password
	}
	if update.RCONPassword != nil {
		config.RCONPassword = *update.RCONPassword
	}
	if update.TVPassword != nil {
		config.TVPassword = *update.TVPassword
	}
	if update.ConfigFile != nil {
		config.ConfigFile = *update.ConfigFile
	}
	if update.EnableLogsTFUpload != nil {
		config.EnableLogsTFUpload = *update.EnableLogsTFUpload
	}
	if update.EnableDemosTFUpload != nil {
		config.EnableDemosTFUpload = *update.EnableDemosTFUpload
	}
	return nil
}
