package tools

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// Bash runs a shell command and streams output.
func (e *Executor) Bash(toolCallID, command string, timeoutSeconds int, onOutput OutputCallback) Result {
	timeout := int(BashTimeout / time.Second)
	if timeoutSeconds > 0 {
		timeout = timeoutSeconds
	}

	cmd := exec.Command("/bin/sh", "-c", command)
	cmd.Dir = e.cwd
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return errorResult("error: "+err.Error(), err.Error())
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return errorResult("error: "+err.Error(), err.Error())
	}
	if err := cmd.Start(); err != nil {
		return errorResult("error: "+err.Error(), err.Error())
	}
	e.trackCmd(cmd)
	defer func() {
		e.untrackCmd(cmd)
		if cmd.ProcessState == nil || !cmd.ProcessState.Exited() {
			killCmdTree(cmd)
		}
	}()

	type pipeLine struct {
		text string
		done bool
	}

	lines := make(chan pipeLine, 256)
	readerErrors := make(chan error, 2)
	readPipe := func(reader io.Reader) {
		defer func() {
			lines <- pipeLine{done: true}
		}()
		scanner := bufio.NewScanner(reader)
		scanner.Buffer(make([]byte, 0, 64*1024), 10*1024*1024)
		for scanner.Scan() {
			lines <- pipeLine{text: strings.TrimRight(scanner.Text(), "\n")}
		}
		if err := scanner.Err(); err != nil {
			readerErrors <- err
		}
	}
	go readPipe(stdout)
	go readPipe(stderr)

	logPath := filepath.Join(e.toolOutputDir, "bash-"+toolCallID+".log")
	keptLines := []string{}
	keptBytes := 0
	totalLineCount := 0
	tailLines := []string{}
	var logFile *os.File
	var savedOutputPath string
	doneReaders := 0
	deadline := time.Now().Add(time.Duration(timeout) * time.Second)

	for doneReaders < 2 {
		if time.Now().After(deadline) {
			killCmdTree(cmd)
			if logFile != nil {
				_ = logFile.Close()
			}
			return Result{
				ModelText:   fmt.Sprintf("error: timeout after %ds", timeout),
				DisplayText: fmt.Sprintf("Command timed out after %ds", timeout),
				IsError:     true,
			}
		}

		select {
		case err := <-readerErrors:
			if logFile != nil {
				_ = logFile.Close()
			}
			return errorResult("error: "+err.Error(), err.Error())
		case line := <-lines:
			if line.done {
				doneReaders++
				continue
			}
			totalLineCount++
			keptBytes += len([]byte(line.text)) + 1

			if logFile == nil {
				keptLines = append(keptLines, line.text)
				if keptBytes > BashMaxInMemoryBytes {
					file, fileErr := os.Create(logPath)
					if fileErr == nil {
						logFile = file
						savedOutputPath = logPath
						if len(keptLines) > 0 {
							_, _ = io.WriteString(logFile, strings.Join(keptLines, "\n"))
							_, _ = io.WriteString(logFile, "\n")
							tailLines = append(tailLines, keptLines...)
							tailLines = trimTailLines(tailLines)
						}
						keptLines = nil
					}
				}
			} else {
				tailLines = append(tailLines, line.text)
				tailLines = trimTailLines(tailLines)
				_, _ = io.WriteString(logFile, line.text)
				_, _ = io.WriteString(logFile, "\n")
			}

			if onOutput != nil {
				onOutput(line.text)
			}
		case <-time.After(100 * time.Millisecond):
		}
	}

	waitErr := cmd.Wait()
	if logFile != nil {
		_ = logFile.Close()
	}
	if waitErr != nil {
		if cmd.ProcessState == nil || !cmd.ProcessState.Exited() {
			return Result{ModelText: "error: cancelled", DisplayText: "Cancelled", IsError: true}
		}
		if exitErr, ok := waitErr.(*exec.ExitError); ok && exitErr.ExitCode() < 0 {
			return Result{ModelText: "error: cancelled", DisplayText: "Cancelled", IsError: true}
		}
	}

	rawOutput := strings.Join(keptLines, "\n")
	if logFile != nil || len(tailLines) > 0 {
		rawOutput = strings.Join(tailLines, "\n")
	}
	output := strings.TrimSpace(rawOutput)
	if output == "" {
		output = "(empty)"
	}
	content, trunc := TruncateText(output, DefaultMaxLines, DefaultMaxBytes, true)
	if savedOutputPath == "" && trunc.Truncated {
		if err := os.WriteFile(logPath, []byte(rawOutput), 0o644); err == nil {
			savedOutputPath = logPath
		}
	}

	result := content
	wasTruncated := savedOutputPath != "" || trunc.Truncated
	if wasTruncated {
		notice := ""
		if trunc.TruncatedBy == "bytes" {
			if totalLineCount <= 1 {
				notice = fmt.Sprintf("[Truncated: showing last %dKB of output (%dKB limit).", DefaultMaxBytes/1024, DefaultMaxBytes/1024)
			} else {
				notice = fmt.Sprintf("[Truncated: showing tail output (%dKB limit).", DefaultMaxBytes/1024)
			}
		} else {
			notice = fmt.Sprintf("[Truncated: last %d of %d lines.", trunc.OutputLines, totalLineCount)
		}
		if savedOutputPath != "" {
			notice += " Full output: " + savedOutputPath + "]"
		} else {
			notice += "]"
		}
		result += "\n\n" + notice
	}

	if cmd.ProcessState != nil && cmd.ProcessState.ExitCode() != 0 {
		result += fmt.Sprintf("\n\n[exit code: %d]", cmd.ProcessState.ExitCode())
	}
	return Result{ModelText: result, DisplayText: result}
}
