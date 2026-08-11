// Package websocket provides a WebSocket client for communicating with the backend.
package websocket

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// Client manages the WebSocket connection to the backend
type Client struct {
	url         string
	token       string
	conn        *websocket.Conn
	messages    chan []byte
	done        chan struct{}
	connectedCh chan struct{}
	closeOnce   sync.Once
	mu          sync.Mutex
	connected   bool
}

// NewClient creates a new WebSocket client
func NewClient(backendURL, token string) *Client {
	return &Client{
		url:         backendURL,
		token:       token,
		messages:    make(chan []byte, 100),
		done:        make(chan struct{}),
		connectedCh: make(chan struct{}, 4),
	}
}

// Connect establishes the WebSocket connection
func (c *Client) Connect() error {
	c.mu.Lock()
	defer c.mu.Unlock()

	// Parse and add token to URL
	u, err := url.Parse(c.url)
	if err != nil {
		return fmt.Errorf("invalid URL: %w", err)
	}

	log.Printf("Connecting to %s://%s%s", u.Scheme, u.Host, u.EscapedPath())

	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+c.token)

	conn, _, err := websocket.DefaultDialer.Dial(u.String(), headers)
	if err != nil {
		return fmt.Errorf("dial failed: %w", err)
	}

	c.conn = conn
	c.connected = true
	select {
	case c.connectedCh <- struct{}{}:
	default:
	}

	// Start read loop
	go c.readLoop(conn)

	log.Println("Connected to backend")
	return nil
}

// Close closes the WebSocket connection
func (c *Client) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()

	c.closeOnce.Do(func() { close(c.done) })
	if c.conn != nil {
		return c.conn.Close()
	}
	return nil
}

// Connections emits after every successful initial connection or reconnect.
func (c *Client) Connections() <-chan struct{} {
	return c.connectedCh
}

// IsConnected reports whether messages can currently be sent.
func (c *Client) IsConnected() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.connected
}

// Messages returns a channel for receiving messages from the backend
func (c *Client) Messages() <-chan []byte {
	return c.messages
}

// Send sends a JSON message to the backend
func (c *Client) Send(msg interface{}) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if !c.connected || c.conn == nil {
		return fmt.Errorf("not connected")
	}

	data, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("marshal failed: %w", err)
	}

	if err := c.conn.WriteMessage(websocket.TextMessage, data); err != nil {
		return fmt.Errorf("write failed: %w", err)
	}

	return nil
}

// SendBootProgress sends a boot progress update
func (c *Client) SendBootProgress(stage string, progress int, message string) error {
	return c.Send(map[string]interface{}{
		"type":     "boot_progress",
		"stage":    stage,
		"progress": progress,
		"message":  message,
	})
}

// SendStatus sends a status heartbeat with optional system stats.
func (c *Client) SendStatus(containerStatus string, uptime int, extra map[string]interface{}) error {
	msg := map[string]interface{}{
		"type":      "status",
		"container": containerStatus,
		"uptime":    uptime,
	}
	for k, v := range extra {
		msg[k] = v
	}
	return c.Send(msg)
}

// SendServerReady notifies the backend that the server is ready
func (c *Client) SendServerReady(ip string, port, tvPort int) error {
	return c.Send(map[string]interface{}{
		"type":    "boot_progress",
		"stage":   "server_ready",
		"ip":      ip,
		"port":    port,
		"tv_port": tvPort,
	})
}

// ServerReadyInfo contains all server address information for the server_ready message.
type ServerReadyInfo struct {
	RealIP     string
	RealPort   int
	RealTVPort int
	SDRIP      string
	SDRPort    int
	SDRTVPort  int
	Map        string
}

// SendServerReadyWithSDR notifies the backend that the server is ready,
// including both real IP and SDR FakeIP addresses.
func (c *Client) SendServerReadyWithSDR(info ServerReadyInfo) error {
	return c.Send(map[string]interface{}{
		"type":         "boot_progress",
		"stage":        "server_ready",
		"real_ip":      info.RealIP,
		"real_port":    info.RealPort,
		"real_tv_port": info.RealTVPort,
		"sdr_ip":       info.SDRIP,
		"sdr_port":     info.SDRPort,
		"sdr_tv_port":  info.SDRTVPort,
		"map":          info.Map,
	})
}

// SendCompetitiveConfigs reports the available competitive config identifiers on the server image.
func (c *Client) SendCompetitiveConfigs(configs []string, containerImage string) error {
	return c.Send(map[string]interface{}{
		"type":            "competitive_configs",
		"configs":         configs,
		"container_image": containerImage,
	})
}

// readLoop continuously reads messages from the WebSocket
func (c *Client) readLoop(conn *websocket.Conn) {
	for {
		select {
		case <-c.done:
			return
		default:
			_, message, err := conn.ReadMessage()
			if err != nil {
				log.Printf("Read error: %v", err)
				c.reconnect()
				return
			}
			c.messages <- message
		}
	}
}

// reconnect attempts to reconnect with exponential backoff
func (c *Client) reconnect() {
	c.mu.Lock()
	c.connected = false
	c.mu.Unlock()

	backoff := time.Second
	maxBackoff := 30 * time.Second

	for {
		select {
		case <-c.done:
			return
		default:
			log.Printf("Reconnecting in %v...", backoff)
			time.Sleep(backoff)

			if err := c.Connect(); err != nil {
				log.Printf("Reconnect failed: %v", err)
				backoff *= 2
				if backoff > maxBackoff {
					backoff = maxBackoff
				}
			} else {
				return
			}
		}
	}
}
