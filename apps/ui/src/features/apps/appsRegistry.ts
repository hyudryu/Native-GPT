import {
  BarChart3,
  Brain,
  DatabaseZap,
  Wrench,
  type LucideIcon,
} from "lucide-react";

export interface AppEntry {
  id: string;
  name: string;
  description: string;
  icon: LucideIcon;
  href: string;
  external?: boolean;
}

export const appsRegistry: AppEntry[] = [
  { id: "analytics", name: "Analytics", description: "Model and token usage", icon: BarChart3, href: "/apps/analytics" },
  { id: "brain", name: "Brain", description: "App-wide agent knowledge", icon: Brain, href: "/apps/brain" },
  { id: "knowledge-dump", name: "Knowledge Dump", description: "Add files, URLs, and notes", icon: DatabaseZap, href: "/apps/knowledge-dump" },
  { id: "tools", name: "Tools", description: "Manage Strands tools", icon: Wrench, href: "/apps/tools" },
];
