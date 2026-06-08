import type { components } from './api-types';

export type Position = components['schemas']['PositionSchema'];
export type PortfolioConfig = components['schemas']['PortfolioConfigSchema'];
export type PlaybookDefinition = components['schemas']['PlaybookDefinitionSchema'];
export type OptionLeg = components['schemas']['OptionLegSchema'];
export type OperationalJournalEntry = components['schemas']['OperationalJournalEntrySchema'];

const API_BASE = '/api';

export async function getPortfolioConfig(): Promise<PortfolioConfig> {
  const res = await fetch(`${API_BASE}/portfolio/config`);
  if (!res.ok) throw new Error('Failed to fetch portfolio config');
  return res.json();
}

export async function updatePortfolioConfig(config: PortfolioConfig): Promise<PortfolioConfig> {
  const res = await fetch(`${API_BASE}/portfolio/config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
  if (!res.ok) throw new Error('Failed to update portfolio config');
  return res.json();
}

export async function getPositions(): Promise<Position[]> {
  const res = await fetch(`${API_BASE}/positions`);
  if (!res.ok) throw new Error('Failed to fetch positions');
  return res.json();
}

export async function getPosition(id: string): Promise<Position> {
  const res = await fetch(`${API_BASE}/positions/${id}`);
  if (!res.ok) throw new Error('Failed to fetch position');
  return res.json();
}

export async function createPosition(pos: Position): Promise<Position> {
  const res = await fetch(`${API_BASE}/positions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(pos),
  });
  if (!res.ok) throw new Error('Failed to create position');
  return res.json();
}

export async function getPlaybooks(): Promise<PlaybookDefinition[]> {
  const res = await fetch(`${API_BASE}/playbooks`);
  if (!res.ok) throw new Error('Failed to fetch playbooks');
  return res.json();
}

export async function createPlaybook(pb: PlaybookDefinition): Promise<PlaybookDefinition> {
  const res = await fetch(`${API_BASE}/playbooks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(pb),
  });
  if (!res.ok) throw new Error('Failed to create playbook');
  return res.json();
}
