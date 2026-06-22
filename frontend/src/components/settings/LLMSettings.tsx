"use client";

import { useEffect, useState } from "react";
import { getLLMSettings, saveLLMSettings, getProviders, getApiKeys } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Loader2, Cpu, Save, AlertTriangle } from "lucide-react";
import { toast } from "sonner";

export function LLMSettings() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [providers, setProviders] = useState<any>({});
  const [apiKeys, setApiKeys] = useState<Record<string, any>>({});
  const [config, setConfig] = useState<any>({ llm_provider: "anthropic", deep_think_llm: "", quick_think_llm: "" });
  const [dirty, setDirty] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [llm, provs, keys]: any[] = await Promise.all([getLLMSettings(), getProviders(), getApiKeys()]);
      setConfig(llm);
      setProviders(provs);
      setApiKeys(keys);
    } catch {
      toast.error("Failed to load LLM settings");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      await saveLLMSettings(config);
      toast.success("LLM settings saved");
      setDirty(false);
    } catch (e: any) {
      toast.error(e.message || "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const updateConfig = (key: string, value: string) => {
    setConfig({ ...config, [key]: value });
    setDirty(true);
  };

  const currentProvider = providers[config.llm_provider];
  // Keyless providers (e.g. Ollama, local) expose requires_key === false.
  const requiresKey = currentProvider?.requires_key !== false;
  const hasKey = !requiresKey || apiKeys[config.llm_provider]?.configured;

  if (loading) {
    return (
      <Card>
        <CardContent className="py-6 text-center text-muted-foreground text-sm">Loading...</CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-lg flex items-center gap-2">
          <Cpu className="h-5 w-5" />
          LLM Provider
        </CardTitle>
        <p className="text-xs text-muted-foreground">
          Choose which AI provider runs your analyses. Different providers have different costs — see the cost guide below.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Provider Selection */}
        <div>
          <label className="text-xs font-medium mb-2 block">Provider</label>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {Object.entries(providers).map(([key, info]: any) => {
              const keyConfigured = apiKeys[key]?.configured;
              const providerRequiresKey = info.requires_key !== false;
              const isSelected = config.llm_provider === key;
              return (
                <button
                  key={key}
                  onClick={() => {
                    const defaultDeep = info.models_deep?.[0] || "";
                    const defaultQuick = info.models_quick?.[0] || "";
                    setConfig({
                      ...config,
                      llm_provider: key,
                      deep_think_llm: defaultDeep,
                      quick_think_llm: defaultQuick,
                    });
                    setDirty(true);
                  }}
                  className={`p-3 rounded-lg border text-left transition-colors ${
                    isSelected
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-primary/30"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium text-sm">{info.name}</span>
                    {!providerRequiresKey ? (
                      <Badge variant="outline" className="bg-blue-50 text-blue-700 border-blue-200 text-[10px]">Local</Badge>
                    ) : keyConfigured ? (
                      <Badge variant="outline" className="bg-green-50 text-green-700 border-green-200 text-[10px]">Key set</Badge>
                    ) : (
                      <Badge variant="outline" className="text-[10px]">No key</Badge>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Key warning */}
        {!hasKey && (
          <div className="p-3 rounded-lg bg-yellow-50 border border-yellow-200 flex items-start gap-2">
            <AlertTriangle className="h-4 w-4 text-yellow-700 flex-shrink-0 mt-0.5" />
            <p className="text-xs text-yellow-800">
              No API key configured for {currentProvider?.name}. Add one in the API Keys section above before running analyses.
            </p>
          </div>
        )}

        {/* Local / keyless provider note */}
        {!requiresKey && currentProvider?.note && (
          <div className="p-3 rounded-lg bg-blue-50 border border-blue-200 flex items-start gap-2">
            <Cpu className="h-4 w-4 text-blue-700 flex-shrink-0 mt-0.5" />
            <p className="text-xs text-blue-800">{currentProvider.note}</p>
          </div>
        )}

        {/* Model selection */}
        {currentProvider && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="text-xs font-medium mb-1 block">
                Deep Think Model
                <span className="text-muted-foreground ml-1 font-normal">(research + portfolio manager)</span>
              </label>
              <select
                value={config.deep_think_llm}
                onChange={(e) => updateConfig("deep_think_llm", e.target.value)}
                className="w-full px-3 py-2 rounded-md border border-border bg-background text-sm"
              >
                {(currentProvider.models_deep || []).map((m: string) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
              <p className="text-[10px] text-muted-foreground mt-1">Used 2 times per analysis. Smarter = better decisions.</p>
            </div>

            <div>
              <label className="text-xs font-medium mb-1 block">
                Quick Think Model
                <span className="text-muted-foreground ml-1 font-normal">(analysts + debates)</span>
              </label>
              <select
                value={config.quick_think_llm}
                onChange={(e) => updateConfig("quick_think_llm", e.target.value)}
                className="w-full px-3 py-2 rounded-md border border-border bg-background text-sm"
              >
                {(currentProvider.models_quick || []).map((m: string) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
              <p className="text-[10px] text-muted-foreground mt-1">Used 13 times per analysis. Faster = cheaper.</p>
            </div>
          </div>
        )}

        {/* Save button */}
        <div className="flex items-center justify-between pt-2">
          <div className="text-xs text-muted-foreground">
            Current: <span className="font-medium text-foreground">{config.llm_provider}</span> / {config.deep_think_llm} + {config.quick_think_llm}
          </div>
          <Button onClick={handleSave} disabled={!dirty || saving}>
            {saving ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Save className="h-3 w-3 mr-1" />}
            Save Changes
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
