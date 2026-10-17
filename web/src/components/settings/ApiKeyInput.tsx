import type { ReactNode } from "react";

import type { ApiKeyDraft } from "@/components/settings/settingsDraft";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const ENV_REF_RE = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/;

interface ApiKeyInputProps {
  draft: ApiKeyDraft;
  /** Typing and clearing both mark the key dirty with the new text. */
  onInput: (text: string) => void;
  clearLabel: string;
  id: string;
}

/** Key input that stands in for a saved secret until the user types or clears it. */
export function ApiKeyInput({ draft, onInput, clearLabel, id }: ApiKeyInputProps) {
  const showsSaved = draft.api_key_saved && !draft.api_key_dirty;
  return (
    <div className="flex items-center gap-2">
      <Input
        id={id}
        value={draft.api_key_input}
        onChange={(e) => onInput(e.target.value)}
        placeholder={
          showsSaved ? "•••••• saved · type to replace" : "paste a key, or use ${ENV_VAR}"
        }
        spellCheck={false}
        autoCorrect="off"
        autoCapitalize="off"
        autoComplete="off"
        className="font-mono"
      />
      {showsSaved && (
        <Button
          variant="ghost-destructive"
          size="lg"
          onClick={() => onInput("")}
          className="px-2 text-xs font-normal"
          aria-label={clearLabel}
        >
          clear
        </Button>
      )}
    </div>
  );
}

/** Hint for a `${VAR}` key input; undefined when the input is not an env reference. */
export function envRefHint(input: string, envByName: Record<string, boolean>): ReactNode {
  const ref = input.trim().match(ENV_REF_RE)?.[1];
  if (!ref) return undefined;
  return envByName[ref] ? (
    <>
      env <code className="font-mono">{ref}</code> is set
    </>
  ) : (
    <span className="text-destructive/80">
      env <code className="font-mono">{ref}</code> is not set
    </span>
  );
}
