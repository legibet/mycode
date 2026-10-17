/** Triggers shown inside the input box bottom row: model, effort. */

import { Check, ChevronDown } from "lucide-react";
import { memo, useState } from "react";

import type { RemoteConfig } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { LocalConfig, ReasoningEffort } from "@/types";
import { cn } from "@/utils/cn";
import { getReasoningEffort, getReasoningEffortOptions, reasoningEffortKey } from "@/utils/config";

const TRIGGER_BTN = "gap-0.5 px-1.5 font-normal";

// Skip auto-focus on touch/pen so the soft keyboard doesn't pop up just
// from opening the picker. `openType` is Base UI's `InteractionType`.
const initialFocusByOpenType = (openType: string) =>
  openType === "mouse" || openType === "keyboard";

interface TriggerProps {
  config: LocalConfig;
  remoteConfig: RemoteConfig | null;
  onUpdateConfig: (config: LocalConfig) => void;
}

export const ModelTrigger = memo(function ModelTrigger({
  config,
  remoteConfig,
  onUpdateConfig,
}: TriggerProps) {
  const [open, setOpen] = useState(false);

  const label = config.model || "no-model";

  const select = (provider: string, model: string) => {
    onUpdateConfig({ ...config, provider, model });
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="xs"
            className={cn(TRIGGER_BTN, "max-w-full min-w-0")}
            title={label}
          >
            <span className="truncate font-mono">{label}</span>
            <ChevronDown
              className={cn(
                "size-3 shrink-0 opacity-60 transition-transform",
                open && "rotate-180",
              )}
            />
          </Button>
        }
      />
      <PopoverContent
        side="top"
        align="start"
        sideOffset={6}
        initialFocus={initialFocusByOpenType}
        className="w-80 max-w-[calc(100vw-1rem)] gap-0 p-0"
      >
        <Command
          defaultValue={`${config.provider}:${config.model}`}
          className="max-h-[min(60vh,360px)] bg-transparent"
        >
          <CommandInput placeholder="Filter models…" />
          <CommandList>
            <CommandEmpty>No matches</CommandEmpty>
            {Object.entries(remoteConfig?.providers ?? {}).map(([providerKey, provider]) => (
              <CommandGroup key={providerKey} heading={provider.name}>
                {provider.models.map((model) => {
                  const active = providerKey === config.provider && model === config.model;
                  return (
                    <CommandItem
                      key={`${providerKey}:${model}`}
                      value={`${providerKey}:${model}`}
                      keywords={[provider.name, model]}
                      onSelect={() => select(providerKey, model)}
                    >
                      <span className="flex-1 truncate font-mono text-ui">{model}</span>
                      {active && <Check className="size-3 shrink-0 text-primary" />}
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
});

export const EffortTrigger = memo(function EffortTrigger({
  config,
  remoteConfig,
  onUpdateConfig,
}: TriggerProps) {
  const [open, setOpen] = useState(false);

  const options = getReasoningEffortOptions(remoteConfig, config.provider, config.model);

  const current = getReasoningEffort(config, remoteConfig);

  if (!options.length) return null;

  const select = (value: ReasoningEffort) => {
    onUpdateConfig({
      ...config,
      reasoningEfforts: {
        ...config.reasoningEfforts,
        [reasoningEffortKey(config.provider, config.model)]: value,
      },
    });
    setOpen(false);
  };

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="xs"
            className={TRIGGER_BTN}
            title={`Reasoning effort: ${current}`}
          >
            <span className="font-mono">{current}</span>
            <ChevronDown
              className={cn(
                "size-3 shrink-0 opacity-60 transition-transform",
                open && "rotate-180",
              )}
            />
          </Button>
        }
      />
      <DropdownMenuContent side="top" align="start" sideOffset={6} className="min-w-35">
        {options.map((opt) => {
          const active = opt === current;
          return (
            <DropdownMenuItem key={opt} onClick={() => select(opt)}>
              <span className="flex-1 font-mono text-ui">{opt || "auto"}</span>
              {active && <Check className="size-3 shrink-0 text-primary" />}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
});
