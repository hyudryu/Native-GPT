import type {
  CreateEndpointInput,
  Endpoint,
  UpdateEndpointInput,
} from "./endpoints";

export interface ProviderPersistence {
  create: (input: CreateEndpointInput) => Promise<Endpoint>;
  update: (id: string, input: UpdateEndpointInput) => Promise<Endpoint>;
}

/**
 * Persist the draft needed by the host's ID-based test route. Once an ID exists,
 * repeated tests update that provider rather than creating duplicate records.
 */
export function persistProviderForTest(
  existing: Endpoint | null,
  input: CreateEndpointInput,
  persistence: ProviderPersistence,
): Promise<Endpoint> {
  return existing
    ? persistence.update(existing.id, input)
    : persistence.create(input);
}
