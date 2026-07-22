import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { socket } from "./ws";

/**
 * Multi-client sync: the host broadcasts `data.changed` envelopes whenever any
 * client (or a background run) mutates conversations, messages, or projects.
 * Invalidate the affected react-query caches so every open window — desktop,
 * second browser, phone — converges without waiting for a focus refetch.
 */
export function useDataChangedSync() {
  const queryClient = useQueryClient();

  useEffect(() => {
    return socket.on("data.changed", (envelope) => {
      const payload = envelope.payload as {
        entity?: string;
        id?: string;
        conversation_id?: string;
      };
      switch (payload.entity) {
        case "message":
          if (payload.conversation_id) {
            void queryClient.invalidateQueries({
              queryKey: ["conversations", payload.conversation_id, "messages"],
            });
          }
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          break;
        case "conversation":
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          if (payload.conversation_id ?? payload.id) {
            void queryClient.invalidateQueries({
              queryKey: ["conversations", payload.conversation_id ?? payload.id],
            });
          }
          break;
        case "project":
          void queryClient.invalidateQueries({ queryKey: ["projects"] });
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          break;
        default:
          // Unknown entity: fall back to refreshing the sidebar data.
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          void queryClient.invalidateQueries({ queryKey: ["projects"] });
      }
    });
  }, [queryClient]);
}
