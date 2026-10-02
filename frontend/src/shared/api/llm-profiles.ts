import type { LlmProfileDto, LlmProfilesDto } from "./backend-types";

export function enabledLlmProfiles(
  profiles: LlmProfilesDto | undefined,
): LlmProfileDto[] {
  return profiles?.providers.filter((profile) => profile.enabled) ?? [];
}

export function selectEnabledLlmProfile(
  profiles: LlmProfilesDto | undefined,
  provider: string,
): LlmProfileDto | null {
  const enabledProfiles = enabledLlmProfiles(profiles);
  return (
    enabledProfiles.find((profile) => profile.provider === provider) ??
    enabledProfiles.find(
      (profile) => profile.provider === profiles?.defaults.provider,
    ) ??
    enabledProfiles[0] ??
    null
  );
}
