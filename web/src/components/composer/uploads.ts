/**
 * Upload attachments from the picker, drag-and-drop and paste: text files
 * become inline snapshots, images and PDFs base64 blocks.
 */

import type { AttachedFile } from "@/types";
import { randomId } from "@/utils/id";

export type UnsupportedMedia = "image" | "document" | "mixed";

interface MediaSupport {
  supportsImages: boolean;
  supportsDocuments: boolean;
}

// File pickers only understand MIME types and extensions, so keep the text
// allowlist explicit here.
const TEXT_FILE_ACCEPT = [
  "text/*",
  ".txt",
  ".md",
  ".mdx",
  ".rst",
  ".json",
  ".jsonl",
  ".yaml",
  ".yml",
  ".toml",
  ".ini",
  ".cfg",
  ".conf",
  ".xml",
  ".html",
  ".htm",
  ".css",
  ".scss",
  ".sass",
  ".less",
  ".js",
  ".jsx",
  ".mjs",
  ".cjs",
  ".ts",
  ".tsx",
  ".mts",
  ".cts",
  ".py",
  ".rb",
  ".php",
  ".go",
  ".rs",
  ".java",
  ".kt",
  ".swift",
  ".c",
  ".cc",
  ".cpp",
  ".cxx",
  ".h",
  ".hh",
  ".hpp",
  ".m",
  ".mm",
  ".sh",
  ".bash",
  ".zsh",
  ".fish",
  ".ps1",
  ".sql",
  ".graphql",
  ".gql",
  ".proto",
  ".csv",
  ".tsv",
  ".log",
  ".env",
  ".gitignore",
  ".gitattributes",
  ".editorconfig",
  ".npmrc",
  ".yarnrc",
  ".pnpmrc",
].join(",");

/** `accept` for the file picker under the current model. */
export function uploadAccept({ supportsImages, supportsDocuments }: MediaSupport): string {
  return [
    TEXT_FILE_ACCEPT,
    supportsImages ? "image/*" : null,
    supportsDocuments ? ".pdf,application/pdf" : null,
  ]
    .filter(Boolean)
    .join(",");
}

export function unsupportedMedia(image: boolean, document: boolean): UnsupportedMedia | null {
  if (image && document) return "mixed";
  if (image) return "image";
  if (document) return "document";
  return null;
}

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      resolve(result.split(",")[1] ?? "");
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function readFileAsUtf8(file: File): Promise<string | null> {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
  } catch {
    return null;
  }
}

/** Media the model cannot accept is dropped and reported; non-UTF-8 text is
 * dropped silently. */
export async function readUploads(
  files: File[],
  { supportsImages, supportsDocuments }: MediaSupport,
): Promise<{ attachments: AttachedFile[]; unsupported: UnsupportedMedia | null }> {
  let unsupportedImage = false;
  let unsupportedDocument = false;
  const attachments = await Promise.all(
    files.map(async (file): Promise<AttachedFile | null> => {
      if (file.type.startsWith("image/")) {
        if (!supportsImages) {
          unsupportedImage = true;
          return null;
        }
        return {
          id: randomId(),
          kind: "image",
          data: await readFileAsBase64(file),
          mime_type: file.type,
          name: file.name,
          preview: URL.createObjectURL(file),
        };
      }

      if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) {
        if (!supportsDocuments) {
          unsupportedDocument = true;
          return null;
        }
        return {
          id: randomId(),
          kind: "document",
          data: await readFileAsBase64(file),
          mime_type: "application/pdf",
          name: file.name,
        };
      }

      const text = await readFileAsUtf8(file);
      if (text === null) return null;
      return { id: randomId(), kind: "text", text, name: file.name };
    }),
  );
  return {
    attachments: attachments.filter((file) => file !== null),
    unsupported: unsupportedMedia(unsupportedImage, unsupportedDocument),
  };
}
