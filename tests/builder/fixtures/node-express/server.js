const express = require('express');
const app = express();
app.use(express.json());
const notes = [];
app.get('/healthz', (req, res) => res.json({ ok: true }));
app.get('/api/notes', (req, res) => res.json(notes));
app.post('/api/notes', (req, res) => { notes.push(String(req.body.text || '')); res.json({ count: notes.length }); });
app.listen(Number(process.env.PORT || 8080));
