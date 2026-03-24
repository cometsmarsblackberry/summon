// Package rcon implements Source RCON protocol for TF2 server commands.
package rcon

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"net"
	"time"
)

const (
	// Packet types
	SERVERDATA_AUTH           = 3
	SERVERDATA_AUTH_RESPONSE  = 2
	SERVERDATA_EXECCOMMAND    = 2
	SERVERDATA_RESPONSE_VALUE = 0
)

// Client is an RCON client
type Client struct {
	conn    net.Conn
	idCount int32
}

// Dial connects to an RCON server
func Dial(address string, password string, timeout time.Duration) (*Client, error) {
	conn, err := net.DialTimeout("tcp", address, timeout)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}

	c := &Client{
		conn:    conn,
		idCount: 0,
	}

	// Authenticate
	if err := c.auth(password); err != nil {
		conn.Close()
		return nil, fmt.Errorf("auth: %w", err)
	}

	return c, nil
}

// Close closes the connection
func (c *Client) Close() error {
	if c.conn != nil {
		return c.conn.Close()
	}
	return nil
}

// Execute sends a command and returns the response
func (c *Client) Execute(command string) (string, error) {
	c.idCount++
	id := c.idCount

	if err := c.writePacket(id, SERVERDATA_EXECCOMMAND, command); err != nil {
		return "", fmt.Errorf("write: %w", err)
	}

	respID, _, body, err := c.readPacket()
	if err != nil {
		return "", fmt.Errorf("read: %w", err)
	}

	if respID != id {
		return "", fmt.Errorf("unexpected response id: got %d, want %d", respID, id)
	}

	return body, nil
}

func (c *Client) auth(password string) error {
	c.idCount++
	id := c.idCount

	if err := c.writePacket(id, SERVERDATA_AUTH, password); err != nil {
		return err
	}

	respID, respType, _, err := c.readPacket()
	if err != nil {
		return err
	}

	// TF2 sends an empty SERVERDATA_RESPONSE_VALUE before the auth response, skip it
	if respType == SERVERDATA_RESPONSE_VALUE {
		respID, respType, _, err = c.readPacket()
		if err != nil {
			return err
		}
	}

	if respType != SERVERDATA_AUTH_RESPONSE {
		return fmt.Errorf("unexpected auth response type: %d", respType)
	}

	if respID == -1 {
		return fmt.Errorf("authentication failed")
	}

	return nil
}

func (c *Client) writePacket(id int32, packetType int32, body string) error {
	bodyBytes := []byte(body)
	// Packet: size(4) + id(4) + type(4) + body(n) + null(1) + null(1)
	size := int32(4 + 4 + len(bodyBytes) + 1 + 1)

	buf := new(bytes.Buffer)
	binary.Write(buf, binary.LittleEndian, size)
	binary.Write(buf, binary.LittleEndian, id)
	binary.Write(buf, binary.LittleEndian, packetType)
	buf.Write(bodyBytes)
	buf.WriteByte(0)
	buf.WriteByte(0)

	c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
	_, err := c.conn.Write(buf.Bytes())
	return err
}

func (c *Client) readPacket() (id int32, packetType int32, body string, err error) {
	c.conn.SetReadDeadline(time.Now().Add(10 * time.Second))

	// Read size
	var size int32
	if err = binary.Read(c.conn, binary.LittleEndian, &size); err != nil {
		return
	}

	// Read rest of packet
	data := make([]byte, size)
	if _, err = c.conn.Read(data); err != nil {
		return
	}

	buf := bytes.NewReader(data)
	binary.Read(buf, binary.LittleEndian, &id)
	binary.Read(buf, binary.LittleEndian, &packetType)

	// Body is everything except the last 2 null bytes
	bodyBytes := data[8 : len(data)-2]
	body = string(bodyBytes)

	return
}
