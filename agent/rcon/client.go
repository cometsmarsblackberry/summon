// Package rcon implements the Source RCON wire protocol: packet framing shared
// by clients and servers, and a client for one persistent connection.
package rcon

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"sync"
)

// Packet types. The protocol reuses 2 for a client's command and for the
// server's answer to an authentication request.
const (
	TypeAuth          int32 = 3
	TypeAuthResponse  int32 = 2
	TypeExecCommand   int32 = 2
	TypeResponseValue int32 = 0
)

const (
	// MaxBodySize is the largest body one packet carries. SRCDS splits a
	// longer response over several packets.
	MaxBodySize = 4096
	// packetOverhead is the request ID, the type, and the two terminators.
	packetOverhead = 4 + 4 + 2
	// maxResponsePacket bounds what the client accepts from the game server,
	// which does not always split exactly at MaxBodySize.
	maxResponsePacket = 2*MaxBodySize + packetOverhead
)

// ErrAuthFailed means the server rejected the password.
var ErrAuthFailed = errors.New("rcon: authentication failed")

// ErrPacketSize means a packet's declared size is outside the accepted range.
var ErrPacketSize = errors.New("rcon: invalid packet size")

// Packet is one RCON packet. Body excludes the terminators.
type Packet struct {
	ID   int32
	Type int32
	Body []byte
}

// WritePacket writes one packet in a single Write call.
func WritePacket(w io.Writer, packet Packet) error {
	if len(packet.Body) > MaxBodySize {
		return ErrPacketSize
	}
	size := packetOverhead + len(packet.Body)
	buffer := make([]byte, 4+size)
	binary.LittleEndian.PutUint32(buffer[0:], uint32(size))
	binary.LittleEndian.PutUint32(buffer[4:], uint32(packet.ID))
	binary.LittleEndian.PutUint32(buffer[8:], uint32(packet.Type))
	copy(buffer[12:], packet.Body)
	_, err := w.Write(buffer)
	return err
}

// ReadPacket reads one packet whose declared size is at most maxSize bytes.
func ReadPacket(r io.Reader, maxSize int) (Packet, error) {
	data, err := readFrame(r, packetOverhead, maxSize)
	if err != nil {
		return Packet{}, err
	}
	return Packet{
		ID:   int32(binary.LittleEndian.Uint32(data[0:])),
		Type: int32(binary.LittleEndian.Uint32(data[4:])),
		Body: data[8 : len(data)-2],
	}, nil
}

// ReadRequest reads one packet sent by a client, as a server does, whose
// declared size is at most maxSize bytes. SRCDS reads a request's body as a
// C string, so the body ends at its first NUL. Some clients end packets with
// more than the two terminators, or with only one, and SRCDS accepts them.
func ReadRequest(r io.Reader, maxSize int) (Packet, error) {
	data, err := readFrame(r, 4+4+1, maxSize)
	if err != nil {
		return Packet{}, err
	}
	body := data[8:]
	if end := bytes.IndexByte(body, 0); end >= 0 {
		body = body[:end]
	}
	return Packet{
		ID:   int32(binary.LittleEndian.Uint32(data[0:])),
		Type: int32(binary.LittleEndian.Uint32(data[4:])),
		Body: body,
	}, nil
}

// readFrame reads one size-prefixed packet and returns what follows the size.
func readFrame(r io.Reader, minSize, maxSize int) ([]byte, error) {
	var header [4]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		return nil, err
	}
	size := int(int32(binary.LittleEndian.Uint32(header[:])))
	if size < minSize || size > maxSize {
		return nil, ErrPacketSize
	}
	data := make([]byte, size)
	if _, err := io.ReadFull(r, data); err != nil {
		if errors.Is(err, io.EOF) {
			err = io.ErrUnexpectedEOF
		}
		return nil, err
	}
	return data, nil
}

// Client is one authenticated RCON connection. Calls must not overlap.
type Client struct {
	conn    io.ReadWriteCloser
	packets chan Packet
	done    chan struct{}
	stop    chan struct{}

	closeOnce sync.Once
	readErr   error
	nextID    int32
}

// NewClient starts reading from conn, which the client owns from now on.
func NewClient(conn io.ReadWriteCloser) *Client {
	client := &Client{
		conn:    conn,
		packets: make(chan Packet, 16),
		done:    make(chan struct{}),
		stop:    make(chan struct{}),
	}
	go client.readLoop()
	return client
}

func (c *Client) readLoop() {
	defer close(c.done)
	for {
		packet, err := ReadPacket(c.conn, maxResponsePacket)
		if err != nil {
			c.readErr = err
			return
		}
		select {
		case c.packets <- packet:
		case <-c.stop:
			c.readErr = io.ErrClosedPipe
			return
		}
	}
}

// Done is closed once the connection has ended, whichever side ended it.
func (c *Client) Done() <-chan struct{} {
	return c.done
}

// Close ends the connection.
func (c *Client) Close() error {
	var err error
	c.closeOnce.Do(func() {
		close(c.stop)
		err = c.conn.Close()
	})
	return err
}

func (c *Client) newID() int32 {
	c.nextID++
	// Request IDs stay positive: -1 is the server's authentication failure.
	if c.nextID <= 0 {
		c.nextID = 1
	}
	return c.nextID
}

func (c *Client) read(ctx context.Context) (Packet, error) {
	select {
	case packet := <-c.packets:
		return packet, nil
	default:
	}
	select {
	case packet := <-c.packets:
		return packet, nil
	case <-c.done:
		// Deliver what arrived before the connection ended.
		select {
		case packet := <-c.packets:
			return packet, nil
		default:
		}
		if c.readErr != nil {
			return Packet{}, fmt.Errorf("rcon: connection closed: %w", c.readErr)
		}
		return Packet{}, io.EOF
	case <-ctx.Done():
		return Packet{}, ctx.Err()
	}
}

// Authenticate sends the password and waits for the server's verdict.
func (c *Client) Authenticate(ctx context.Context, password string) error {
	id := c.newID()
	if err := WritePacket(c.conn, Packet{ID: id, Type: TypeAuth, Body: []byte(password)}); err != nil {
		return fmt.Errorf("rcon: write: %w", err)
	}
	for {
		packet, err := c.read(ctx)
		if err != nil {
			return err
		}
		// SRCDS sends an empty response value before the verdict.
		if packet.Type != TypeAuthResponse {
			continue
		}
		if packet.ID == -1 {
			return ErrAuthFailed
		}
		if packet.ID == id {
			return nil
		}
	}
}

// Execute runs one command and returns its complete output.
//
// A response has no end marker and may span several packets, so an empty
// response value follows the command. SRCDS answers requests in order and
// mirrors that packet back, which marks the end of the command's output.
func (c *Client) Execute(ctx context.Context, command string) (string, error) {
	if len(command) > MaxBodySize {
		return "", ErrPacketSize
	}
	commandID := c.newID()
	endID := c.newID()
	if err := WritePacket(c.conn, Packet{ID: commandID, Type: TypeExecCommand, Body: []byte(command)}); err != nil {
		return "", fmt.Errorf("rcon: write: %w", err)
	}
	if err := WritePacket(c.conn, Packet{ID: endID, Type: TypeResponseValue}); err != nil {
		return "", fmt.Errorf("rcon: write: %w", err)
	}

	var output []byte
	for {
		packet, err := c.read(ctx)
		if err != nil {
			return "", err
		}
		switch packet.ID {
		case commandID:
			output = append(output, packet.Body...)
		case endID:
			return string(output), nil
		}
		// Anything else is the tail of an earlier exchange.
	}
}
