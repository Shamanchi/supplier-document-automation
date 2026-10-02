import assert from "node:assert/strict";
import test from "node:test";

import {
  enabledLlmProfiles,
  selectEnabledLlmProfile,
} from "../src/shared/api/llm-profiles.ts";
import { buildPriceImportFormData } from "../src/shared/api/price-import-form.ts";
import type { LlmProfilesDto } from "../src/shared/api/backend-types.ts";

const profiles: LlmProfilesDto = {
  providers: [
    {
      provider: "openai",
      label: "OpenAI-compatible",
      enabled: false,
      models: ["gpt-4o"],
      default_model: "gpt-4o",
      supports_thinking: false,
    },
    {
      provider: "yandexgpt",
      label: "Yandex Cloud",
      enabled: true,
      models: ["qwen3-235b-a22b-fp8/latest"],
      default_model: "qwen3-235b-a22b-fp8/latest",
      supports_thinking: false,
    },
  ],
  defaults: { provider: "openai" },
};

test("import request sends the selected provider and model", () => {
  const form = buildPriceImportFormData({
    file: new File(["price"], "price.xlsx"),
    supplierExternalId: "supplier-42",
    supplierName: "Поставщик 42",
    currencyHint: "KZT",
    provider: "yandexgpt",
    model: "qwen3-235b-a22b-fp8/latest",
    idempotencyKey: "import-42",
  });

  assert.equal(form.get("supplier_external_id"), "supplier-42");
  assert.equal(form.get("supplier_name"), "Поставщик 42");
  assert.equal(form.get("currency_hint"), "KZT");
  assert.equal(form.get("provider"), "yandexgpt");
  assert.equal(form.get("model"), "qwen3-235b-a22b-fp8/latest");
  assert.equal(form.get("file") instanceof File, true);
});

test("profile selector ignores disabled providers and chooses an enabled profile", () => {
  assert.deepEqual(
    enabledLlmProfiles(profiles).map((profile) => profile.provider),
    ["yandexgpt"],
  );
  assert.equal(
    selectEnabledLlmProfile(profiles, "openai")?.provider,
    "yandexgpt",
  );
});
