import { EventEmitter } from 'node:events';

export const whatsappEvents = new EventEmitter();
whatsappEvents.setMaxListeners(32);
