import { useMemo } from 'react';

export function groupConversationHistory(conversations) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const previousWeek = new Date(today);
  previousWeek.setDate(previousWeek.getDate() - 7);
  const groups = [
    { label: 'Today', conversations: [] },
    { label: 'Previous 7 Days', conversations: [] },
    { label: 'Earlier', conversations: [] },
  ];
  for (const conversation of conversations) {
    const updatedAt = new Date(conversation.updated_at || conversation.created_at);
    const group = Number.isNaN(updatedAt.getTime()) || updatedAt < previousWeek
      ? groups[2] : updatedAt >= today ? groups[0] : groups[1];
    group.conversations.push(conversation);
  }
  return groups.filter((group) => group.conversations.length);
}

export default function useConversations(conversations) {
  return useMemo(() => groupConversationHistory(conversations), [conversations]);
}
