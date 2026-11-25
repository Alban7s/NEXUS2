import React from 'react';
import { createRoot } from 'react-dom/client';
import { greeting } from './greeting.js';

createRoot(document.getElementById('root')).render(<h1>{greeting('NEXUS')}</h1>);
