//go:build embedweb

package server

import (
	"embed"
	"io/fs"
)

//go:embed all:webdist
var embeddedWebFiles embed.FS

func embeddedWebFS() fs.FS {
	sub, err := fs.Sub(embeddedWebFiles, "webdist")
	if err != nil {
		return nil
	}
	return sub
}
