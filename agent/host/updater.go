package host

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var sha256Pattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type agentManifest struct {
	Version                string `json:"version"`
	ProtocolMin            int    `json:"protocol_min"`
	ProtocolMax            int    `json:"protocol_max"`
	DownloadURL            string `json:"download_url"`
	SHA256                 string `json:"sha256"`
	RollbackTimeoutSeconds int    `json:"rollback_timeout_seconds"`
}

type pendingUpdate struct {
	TargetPath  string    `json:"target_path"`
	BackupPath  string    `json:"backup_path"`
	FromVersion string    `json:"from_version"`
	ToVersion   string    `json:"to_version"`
	Deadline    time.Time `json:"deadline"`
}

type failedUpdate struct {
	Version  string    `json:"version"`
	FailedAt time.Time `json:"failed_at"`
}

func pendingUpdatePath(stateDir string) string {
	return filepath.Join(stateDir, "pending-agent-update.json")
}

func failedUpdatePath(stateDir string) string {
	return filepath.Join(stateDir, "failed-agent-update.json")
}

func (c *Controller) sendUpdateStatus(status string, draining bool, err error) {
	message := map[string]any{
		"type": "host.update", "protocol": ProtocolVersion,
		"status": status, "draining": draining, "agent_version": c.config.AgentVersion,
	}
	if err != nil {
		message["error"] = err.Error()
	}
	_ = c.transport.Send(message)
}

func (c *Controller) requestUpdate(manifestURL, versionPin string, forced bool) {
	c.updateMu.Lock()
	defer c.updateMu.Unlock()
	manifestURL = strings.TrimSpace(manifestURL)
	if manifestURL == "" {
		return
	}
	c.sendUpdateStatus("checking", false, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	manifest, err := c.fetchManifest(ctx, manifestURL)
	cancel()
	if err != nil {
		c.sendUpdateStatus("failed", false, err)
		return
	}
	if manifest.ProtocolMin > ProtocolMax || manifest.ProtocolMax < ProtocolMin {
		c.sendUpdateStatus("failed", false, fmt.Errorf("release protocol is incompatible with this agent"))
		return
	}
	if versionPin != "" && manifest.Version != versionPin {
		if c.config.AgentVersion == versionPin {
			c.sendUpdateStatus("current", false, nil)
			return
		}
		c.sendUpdateStatus("failed", false, fmt.Errorf("pinned version %q is not present in the deployed release", versionPin))
		return
	}
	if manifest.Version == "" || manifest.Version == c.config.AgentVersion {
		c.sendUpdateStatus("current", false, nil)
		return
	}
	if !forced && failedUpdateVersion(c.config.StateDir) == manifest.Version {
		c.sendUpdateStatus(
			"retry_required", false,
			fmt.Errorf("agent version %s previously failed; retry it from the admin panel or deploy a newer release", manifest.Version),
		)
		return
	}
	if !forced && c.config.AgentVersion == "dev" {
		// Development builds do not replace themselves merely because a local
		// backend reports a release version. An explicit admin retry still works.
		c.sendUpdateStatus("current", false, nil)
		return
	}

	c.updateDraining.Store(true)
	c.sendUpdateStatus("waiting_for_idle", true, nil)
	c.waitForSlotOperationBarrier()
	for !c.isIdle() {
		select {
		case <-c.ctx.Done():
			c.updateDraining.Store(false)
			c.sendUpdateStatus("failed", false, c.ctx.Err())
			return
		case <-time.After(5 * time.Second):
		}
	}
	c.sendUpdateStatus("downloading", true, nil)
	if err := c.stageAndActivate(manifest); err != nil {
		c.updateDraining.Store(false)
		_ = markFailedUpdate(c.config.StateDir, manifest.Version)
		c.sendUpdateStatus("failed", false, err)
		return
	}
	c.sendUpdateStatus("activating", true, nil)
	c.restart()
}

func (c *Controller) fetchManifest(ctx context.Context, manifestURL string) (*agentManifest, error) {
	parsed, err := url.Parse(manifestURL)
	if err != nil || !allowedUpdateURL(parsed) {
		return nil, fmt.Errorf("invalid authenticated manifest URL")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, parsed.String(), nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+c.config.Credential)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("manifest returned HTTP %d", response.StatusCode)
	}
	var manifest agentManifest
	decoder := json.NewDecoder(io.LimitReader(response.Body, 1<<20))
	if err := decoder.Decode(&manifest); err != nil {
		return nil, fmt.Errorf("decode update manifest: %w", err)
	}
	if manifest.Version == "" || !sha256Pattern.MatchString(strings.ToLower(manifest.SHA256)) {
		return nil, fmt.Errorf("update manifest is incomplete")
	}
	download, err := url.Parse(manifest.DownloadURL)
	if err != nil || !allowedUpdateURL(download) {
		return nil, fmt.Errorf("invalid agent download URL")
	}
	if manifest.RollbackTimeoutSeconds < 30 || manifest.RollbackTimeoutSeconds > 300 {
		manifest.RollbackTimeoutSeconds = 90
	}
	return &manifest, nil
}

func allowedUpdateURL(parsed *url.URL) bool {
	if parsed == nil {
		return false
	}
	if parsed.Scheme == "https" {
		return parsed.Host != ""
	}
	if parsed.Scheme != "http" {
		return false
	}
	host := parsed.Hostname()
	return host == "localhost" || host == "127.0.0.1" || host == "::1"
}

func (c *Controller) stageAndActivate(manifest *agentManifest) error {
	executable, err := os.Executable()
	if err != nil {
		return fmt.Errorf("resolve current executable: %w", err)
	}
	executable, err = filepath.EvalSymlinks(executable)
	if err != nil {
		return fmt.Errorf("resolve executable symlink: %w", err)
	}
	stagePath := executable + ".next"
	backupPath := executable + ".previous"
	_ = os.Remove(stagePath)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, manifest.DownloadURL, nil)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+c.config.Credential)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return fmt.Errorf("download agent: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("agent download returned HTTP %d", response.StatusCode)
	}
	file, err := os.OpenFile(stagePath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0755)
	if err != nil {
		return fmt.Errorf("create staged agent: %w", err)
	}
	hash := sha256.New()
	limited := &io.LimitedReader{R: response.Body, N: (256 << 20) + 1}
	written, copyErr := io.Copy(io.MultiWriter(file, hash), limited)
	closeErr := file.Close()
	if copyErr != nil {
		_ = os.Remove(stagePath)
		return fmt.Errorf("write staged agent: %w", copyErr)
	}
	if closeErr != nil {
		_ = os.Remove(stagePath)
		return fmt.Errorf("close staged agent: %w", closeErr)
	}
	if written > 256<<20 {
		_ = os.Remove(stagePath)
		return fmt.Errorf("agent download exceeds 256 MiB")
	}
	digest := hex.EncodeToString(hash.Sum(nil))
	if digest != strings.ToLower(manifest.SHA256) {
		_ = os.Remove(stagePath)
		return fmt.Errorf("agent SHA-256 does not match authenticated manifest")
	}
	if err := os.Chmod(stagePath, 0755); err != nil {
		_ = os.Remove(stagePath)
		return err
	}
	selfCheckContext, selfCheckCancel := context.WithTimeout(context.Background(), 20*time.Second)
	selfCheck := exec.CommandContext(selfCheckContext, stagePath, "--self-check")
	output, err := selfCheck.CombinedOutput()
	selfCheckCancel()
	if err != nil {
		_ = os.Remove(stagePath)
		return fmt.Errorf("staged agent self-check failed: %w (%s)", err, strings.TrimSpace(string(output)))
	}

	_ = os.Remove(backupPath)
	if err := os.Rename(executable, backupPath); err != nil {
		_ = os.Remove(stagePath)
		return fmt.Errorf("create agent rollback copy: %w", err)
	}
	if err := os.Rename(stagePath, executable); err != nil {
		_ = os.Rename(backupPath, executable)
		return fmt.Errorf("activate agent update: %w", err)
	}
	pending := pendingUpdate{
		TargetPath: executable, BackupPath: backupPath,
		FromVersion: c.config.AgentVersion, ToVersion: manifest.Version,
		Deadline: time.Now().Add(time.Duration(manifest.RollbackTimeoutSeconds) * time.Second),
	}
	data, err := json.MarshalIndent(pending, "", "  ")
	if err != nil {
		_ = restoreAgentBinary(pending)
		return err
	}
	if err := os.WriteFile(pendingUpdatePath(c.config.StateDir), data, 0600); err != nil {
		_ = restoreAgentBinary(pending)
		return fmt.Errorf("persist update rollback state: %w", err)
	}
	return nil
}

func (c *Controller) completePendingUpdate() {
	pending, err := readPendingUpdate(c.config.StateDir)
	if err != nil || pending == nil {
		return
	}
	if pending.ToVersion != "" && c.config.AgentVersion != pending.ToVersion {
		c.sendUpdateStatus("failed", true, fmt.Errorf("updated binary reported unexpected version %q", c.config.AgentVersion))
		if err := RollbackPendingUpdate(c.config.StateDir); err != nil {
			c.sendUpdateStatus("rollback_failed", true, err)
			return
		}
		c.sendUpdateStatus("rolled_back", false, nil)
		c.restart()
		return
	}
	c.configurationMu.RLock()
	preflightOK := c.preflightOK
	c.configurationMu.RUnlock()
	if !preflightOK {
		c.sendUpdateStatus("failed", true, fmt.Errorf("updated agent failed host preflight"))
		if err := RollbackPendingUpdate(c.config.StateDir); err != nil {
			c.sendUpdateStatus("rollback_failed", true, err)
			return
		}
		c.sendUpdateStatus("rolled_back", false, nil)
		c.restart()
		return
	}
	_ = os.Remove(pending.BackupPath)
	_ = os.Remove(pendingUpdatePath(c.config.StateDir))
	_ = os.Remove(failedUpdatePath(c.config.StateDir))
	c.sendUpdateStatus("ready", false, nil)
}

func failedUpdateVersion(stateDir string) string {
	data, err := os.ReadFile(failedUpdatePath(stateDir))
	if err != nil {
		return ""
	}
	var failed failedUpdate
	if json.Unmarshal(data, &failed) != nil {
		return ""
	}
	return failed.Version
}

func markFailedUpdate(stateDir, version string) error {
	if strings.TrimSpace(version) == "" {
		return nil
	}
	data, err := json.MarshalIndent(failedUpdate{
		Version: version, FailedAt: time.Now(),
	}, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(failedUpdatePath(stateDir), data, 0600)
}

func readPendingUpdate(stateDir string) (*pendingUpdate, error) {
	data, err := os.ReadFile(pendingUpdatePath(stateDir))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var pending pendingUpdate
	if err := json.Unmarshal(data, &pending); err != nil {
		return nil, err
	}
	if pending.TargetPath == "" || pending.BackupPath == "" {
		return nil, fmt.Errorf("pending update state is incomplete")
	}
	return &pending, nil
}

// PendingUpdateDeadline returns the handshake deadline for a newly activated
// binary. Host startup uses it to decide when to restore the previous release.
func PendingUpdateDeadline(stateDir string) (time.Time, bool) {
	pending, err := readPendingUpdate(stateDir)
	if err != nil || pending == nil {
		return time.Time{}, false
	}
	return pending.Deadline, true
}

// RollbackPendingUpdate atomically restores the previous executable.
func RollbackPendingUpdate(stateDir string) error {
	pending, err := readPendingUpdate(stateDir)
	if err != nil || pending == nil {
		return err
	}
	if err := restoreAgentBinary(*pending); err != nil {
		return err
	}
	if err := markFailedUpdate(stateDir, pending.ToVersion); err != nil {
		return err
	}
	return os.Remove(pendingUpdatePath(stateDir))
}

func restoreAgentBinary(pending pendingUpdate) error {
	failedPath := pending.TargetPath + ".failed"
	_ = os.Remove(failedPath)
	if err := os.Rename(pending.TargetPath, failedPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err := os.Rename(pending.BackupPath, pending.TargetPath); err != nil {
		_ = os.Rename(failedPath, pending.TargetPath)
		return err
	}
	_ = os.Remove(failedPath)
	return nil
}
