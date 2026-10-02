import { PlusOutlined } from '@ant-design/icons';
import { App, Button, Input, Modal, Select, Space } from 'antd';
import { useState } from 'react';
import { useProject } from './ProjectContext';

export default function ProjectSelector({ isAdmin }: { isAdmin: boolean }) {
  const { message } = App.useApp();
  const { projects, project, selectProject, createProject } = useProject();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [saving, setSaving] = useState(false);

  return (
    <>
      <Space.Compact>
        <Select
          aria-label="Текущий проект"
          value={project.id}
          style={{ width: 250 }}
          onChange={selectProject}
          options={projects.map((row) => ({
            value: row.id,
            label: `${row.name}${row.is_archived ? ' · архив' : ''}`,
          }))}
        />
        {isAdmin && (
          <Button aria-label="Создать проект" icon={<PlusOutlined />} onClick={() => setOpen(true)} />
        )}
      </Space.Compact>
      <Modal
        title="Новый проект"
        open={open}
        okText="Создать"
        cancelText="Отмена"
        confirmLoading={saving}
        okButtonProps={{ disabled: !name.trim() || !slug.trim() }}
        onCancel={() => setOpen(false)}
        onOk={async () => {
          setSaving(true);
          try {
            await createProject({ name: name.trim(), slug: slug.trim() });
            setOpen(false);
            setName('');
            setSlug('');
            void message.success('Проект создан');
          } finally {
            setSaving(false);
          }
        }}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Название" />
          <Input value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="Код: latin-kebab-case" />
        </Space>
      </Modal>
    </>
  );
}
