package main

import (
	"embed"
	"log"

	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/menu"
	"github.com/wailsapp/wails/v2/pkg/menu/keys"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
	"github.com/wailsapp/wails/v2/pkg/options/mac"
)

//go:embed all:frontend/dist
var assets embed.FS

func main() {
	app := NewApp()

	err := wails.Run(&options.App{
		Title:  "mycode",
		Width:  1280,
		Height: 860,
		AssetServer: &assetserver.Options{
			Assets: assets,
		},
		// Covers the brief gap between NSWindow creation and first webview
		// paint; matches the web UI's dark-theme --background so launch does
		// not flash white.
		BackgroundColour: &options.RGBA{R: 0x0A, G: 0x16, B: 0x28, A: 255},
		Menu:             buildMenu(app),
		OnStartup:        app.startup,
		OnShutdown:       app.shutdown,
		DragAndDrop: &options.DragAndDrop{
			DisableWebViewDrop: true,
		},
		Bind: []any{
			app,
		},
		Mac: &mac.Options{
			TitleBar:   mac.TitleBarHidden(),
			Appearance: mac.DefaultAppearance,
			About: &mac.AboutInfo{
				Title:   "mycode",
				Message: "Personal minimal coding agent.\nWails desktop build.",
			},
		},
	})
	if err != nil {
		log.Fatal(err)
	}
}

func buildMenu(app *App) *menu.Menu {
	result := menu.NewMenu()
	result.Append(menu.AppMenu())

	fileMenu := result.AddSubmenu("File")
	fileMenu.AddText("New Chat", keys.CmdOrCtrl("n"), func(_ *menu.CallbackData) {
		app.emitDesktopCommand("new_chat")
	})
	fileMenu.AddText("Select Workspace", keys.CmdOrCtrl("o"), func(_ *menu.CallbackData) {
		app.emitDesktopCommand("select_workspace")
	})
	fileMenu.AddSeparator()
	fileMenu.AddText("Settings", keys.CmdOrCtrl(","), func(_ *menu.CallbackData) {
		app.emitDesktopCommand("open_settings")
	})

	result.Append(menu.EditMenu())
	result.Append(menu.WindowMenu())
	return result
}
