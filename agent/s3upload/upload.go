// Package s3upload handles uploading TF2 server logs to S3-compatible storage.
package s3upload

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// Config holds S3-compatible storage settings.
type Config struct {
	Endpoint  string
	AccessKey string
	SecretKey string
	Bucket    string
	Region    string
}

// UploadDirectoryTarGz compresses localDir into a .tar.gz and uploads it as a
// single object to the S3 bucket. The key is "{prefix}.tar.gz".
func UploadDirectoryTarGz(ctx context.Context, cfg *Config, localDir, prefix string) error {
	// Create temp .tar.gz file
	tmpFile, err := os.CreateTemp("", "tf2-logs-*.tar.gz")
	if err != nil {
		return fmt.Errorf("create temp archive: %w", err)
	}
	defer os.Remove(tmpFile.Name())
	defer tmpFile.Close()

	// Write tar.gz
	gw := gzip.NewWriter(tmpFile)
	tw := tar.NewWriter(gw)

	var fileCount int
	err = filepath.Walk(localDir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err
		}

		relPath, err := filepath.Rel(localDir, path)
		if err != nil {
			return err
		}

		header, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		header.Name = filepath.ToSlash(relPath)

		if err := tw.WriteHeader(header); err != nil {
			return err
		}

		f, err := os.Open(path)
		if err != nil {
			return err
		}
		defer f.Close()

		if _, err := io.Copy(tw, f); err != nil {
			return err
		}

		fileCount++
		return nil
	})
	if err != nil {
		return fmt.Errorf("create tar.gz: %w", err)
	}

	if err := tw.Close(); err != nil {
		return fmt.Errorf("close tar writer: %w", err)
	}
	if err := gw.Close(); err != nil {
		return fmt.Errorf("close gzip writer: %w", err)
	}

	if fileCount == 0 {
		log.Println("S3 log upload: no log files found, skipping")
		return nil
	}

	// Upload to S3
	endpoint := cfg.Endpoint
	useSSL := true

	if strings.Contains(endpoint, "://") {
		u, err := url.Parse(endpoint)
		if err != nil {
			return fmt.Errorf("parse S3 endpoint: %w", err)
		}
		endpoint = u.Host
		useSSL = u.Scheme == "https"
	}

	client, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(cfg.AccessKey, cfg.SecretKey, ""),
		Secure: useSSL,
		Region: cfg.Region,
	})
	if err != nil {
		return fmt.Errorf("create S3 client: %w", err)
	}

	key := prefix + ".tar.gz"

	info, err := client.FPutObject(ctx, cfg.Bucket, key, tmpFile.Name(), minio.PutObjectOptions{
		ContentType: "application/gzip",
	})
	if err != nil {
		return fmt.Errorf("upload to S3: %w", err)
	}

	log.Printf("S3 upload complete: %d files, %d bytes -> s3://%s/%s", fileCount, info.Size, cfg.Bucket, key)
	return nil
}
