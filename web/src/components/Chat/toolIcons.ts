import {
  FileText,
  Globe,
  type LucideIcon,
  PenLine,
  Search,
  SquarePen,
  Terminal,
} from "lucide-react";

export const TOOL_ICON: Record<string, LucideIcon> = {
  bash: Terminal,
  read: FileText,
  write: PenLine,
  edit: SquarePen,
  webfetch: Globe,
  websearch: Search,
};
