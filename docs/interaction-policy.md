# FSM interaction policy

Button-only transitions may reuse the same card. A transition that expects the next answer as typed text or an attachment must send a new prompt message. This preserves the order: bot question -> user answer -> next bot question.

Citizen appeal flow:

- locality and topic button screens may be cards;
- after manual address input, the next topic prompt is a new message;
- followups are allowed only for `new` and `in_progress` appeals;
- `answered` and `closed` appeals are terminal for followups and use a repeat/feedback appeal instead.

Admin flow:

- navigation/status screens may be edited;
- prompts for broadcast text, operator name, or operator reply are new messages;
- progress messages may still be edited because they do not wait for typed input.

Repeat/feedback appeal:

- a repeat created from `answered` or `closed` keeps a source reference in the summary;
- the topic is marked as feedback, for example: `<topic>: обратная связь по отвеченному вопросу`.