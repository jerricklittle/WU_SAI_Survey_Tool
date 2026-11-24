import secrets

from sqlalchemy import String, Text, Boolean, Integer, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import Column
from sqlalchemy.orm import relationship
from datetime import datetime
from Base import Base


def generate_survey_public_id() -> str:
    """Opaque, URL-safe token for public survey links (not sequential)."""
    return secrets.token_urlsafe(16)

class Survey(Base):
    """Survey model - represents a complete survey with metadata"""
    __tablename__ = "surveys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Public URL token; internal integer id stays for FKs and admin routes.
    public_id = Column(String(64), unique=True, nullable=True, index=True)
    name = Column(String(255), nullable=False)  # Survey name
    description = Column(Text, nullable=True)
    # Rich HTML (e.g. from ui.editor): instructions, informed consent summary, links — shown before questions.
    participant_landing_html = Column(Text, nullable=True)
    version = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)  # Who owns this survey
    settings = Column(JSONB, nullable=True)  # Store survey settings like allowBack, etc.
    # Optional wave or round label, e.g. "Wave 1". Not shown in the respondent URL.
    semester = Column(String(64), nullable=True)
    # Naive UTC: first moment the survey accepts responses; None = no start restriction.
    opens_at = Column(DateTime, nullable=True)
    # Naive UTC: last moment responses are accepted; None = no end restriction.
    closes_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship to survey_questions (junction table)
    survey_questions = relationship("SurveyQuestion", back_populates="survey", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Survey(id={self.id}, name='{self.name}', version={self.version})>"


class QuestionBank(Base):
    """Question Bank - reusable questions that can be added to multiple surveys"""
    __tablename__ = "question_bank"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)  # Short name/identifier for the question
    question_text = Column(Text, nullable=False)  # The actual question text/prompt
    question_type = Column(String(50), nullable=False)  # 'likert', 'boolean', 'text'
    version = Column(Integer, default=1)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)  # Who created this question
    config = Column(JSONB, nullable=False)  # Store type-specific config (options, labels, char limits, etc.)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship to survey_questions
    survey_questions = relationship("SurveyQuestion", back_populates="question")
    collection_items = relationship(
        "QuestionCollectionItem",
        back_populates="question",
        passive_deletes=True,
    )
    
    def __repr__(self):
        return f"<QuestionBank(id={self.id}, name='{self.name}', type='{self.question_type}')>"


class QuestionCollection(Base):
    """Named question bank: an ordered group of reusable questions."""
    __tablename__ = "question_collections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items = relationship(
        "QuestionCollectionItem",
        back_populates="collection",
        cascade="all, delete-orphan",
        order_by="QuestionCollectionItem.sort_order",
    )

    def __repr__(self):
        return f"<QuestionCollection(id={self.id}, name='{self.name}')>"


class QuestionCollectionItem(Base):
    """Membership of a question in a named bank, with display order."""
    __tablename__ = "question_collection_items"
    __table_args__ = (
        UniqueConstraint('collection_id', 'question_id', name='uq_collection_question'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(Integer, ForeignKey('question_collections.id', ondelete='CASCADE'), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey('question_bank.id', ondelete='CASCADE'), nullable=False, index=True)
    sort_order = Column(Integer, nullable=False)

    collection = relationship("QuestionCollection", back_populates="items")
    question = relationship("QuestionBank", back_populates="collection_items")

    def __repr__(self):
        return (
            f"<QuestionCollectionItem(collection_id={self.collection_id}, "
            f"question_id={self.question_id}, sort_order={self.sort_order})>"
        )


class SurveyQuestion(Base):
    """Junction table - links surveys to questions with ordering"""
    __tablename__ = "survey_questions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    survey_id = Column(Integer, ForeignKey('surveys.id'), nullable=False)
    question_id = Column(Integer, ForeignKey('question_bank.id'), nullable=False)
    order = Column(Integer, nullable=False)  # Order of question in survey
    part_id = Column(Integer, ForeignKey('survey_parts.id', ondelete='SET NULL'), nullable=True)
    is_adaptive = Column(Boolean, default=False)  # Whether this question triggers follow-ups
    
    # Relationships
    survey = relationship("Survey", back_populates="survey_questions")
    question = relationship("QuestionBank", back_populates="survey_questions")
    
    def __repr__(self):
        return f"<SurveyQuestion(survey_id={self.survey_id}, question_id={self.question_id}, order={self.order})>"


class SurveyArm(Base):
    """Treatment arm for one survey instrument. Assigned per participant link, not per class survey."""
    __tablename__ = "survey_arms"
    __table_args__ = (
        UniqueConstraint('survey_id', 'key', name='uq_survey_arm_key'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    survey_id = Column(Integer, ForeignKey('surveys.id', ondelete='CASCADE'), nullable=False, index=True)
    key = Column(String(64), nullable=False)
    label = Column(String(255), nullable=False)
    llm_followups = Column(Boolean, nullable=False, default=False)
    external_note_html = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=1)

    survey = relationship("Survey")

    def __repr__(self):
        return f"<SurveyArm(survey_id={self.survey_id}, key='{self.key}')>"


class SurveyPart(Base):
    """A block of questions with its own submit button."""
    __tablename__ = "survey_parts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    survey_id = Column(Integer, ForeignKey('surveys.id', ondelete='CASCADE'), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    submit_label = Column(String(80), nullable=False, default='Submit')
    # This part writes follow-up questions from the open-ended answers in the part before it.
    llm_followups = Column(Boolean, nullable=False, default=False)
    llm_question_count = Column(Integer, nullable=False, default=3)
    llm_instructions = Column(Text, nullable=True)
    # Shown once, before this part starts.
    context_text = Column(Text, nullable=True)
    link_url = Column(Text, nullable=True)
    is_link = Column(Boolean, nullable=False, default=False)
    one_per_page = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=1)

    survey = relationship("Survey")

    def __repr__(self):
        return f"<SurveyPart(survey_id={self.survey_id}, title='{self.title}')>"


class SurveyPartArm(Base):
    """Which treatment arms include a part."""
    __tablename__ = "survey_part_arms"

    part_id = Column(Integer, ForeignKey('survey_parts.id', ondelete='CASCADE'), primary_key=True)
    arm_id = Column(Integer, ForeignKey('survey_arms.id', ondelete='CASCADE'), primary_key=True)
    sort_order = Column(Integer, nullable=False, default=1)
    one_per_page = Column(Boolean, nullable=False, default=True)


class CourseSection(Base):
    """One group of respondents. People are not named here."""
    __tablename__ = "course_sections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    survey_id = Column(Integer, ForeignKey('surveys.id', ondelete='CASCADE'), nullable=False, index=True)
    instructor_name = Column(String(255), nullable=False)
    course_code = Column(String(64), nullable=False)
    section_code = Column(String(64), nullable=False)
    enrollment = Column(Integer, nullable=False, default=0)

    survey = relationship("Survey")

    def __repr__(self):
        return (
            f"<CourseSection(survey_id={self.survey_id}, course='{self.course_code}', "
            f"section='{self.section_code}')>"
        )


class ParticipantLink(Base):
    """Opaque respondent URL. Group and comparison arm live on this row, not in the link."""
    __tablename__ = "participant_links"
    __table_args__ = (
        UniqueConstraint('section_id', 'seat_index', name='uq_section_seat'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    survey_id = Column(Integer, ForeignKey('surveys.id', ondelete='CASCADE'), nullable=False, index=True)
    section_id = Column(Integer, ForeignKey('course_sections.id', ondelete='CASCADE'), nullable=False, index=True)
    arm_id = Column(Integer, ForeignKey('survey_arms.id'), nullable=False)
    seat_index = Column(Integer, nullable=False)

    survey = relationship("Survey")
    section = relationship("CourseSection")
    arm = relationship("SurveyArm")

    def __repr__(self):
        return f"<ParticipantLink(section_id={self.section_id}, seat_index={self.seat_index})>"


# Example config structures for different question types:
"""
LIKERT CONFIG:
{
    "scale": {
        "labels": {
            "1": "Strongly Disagree",
            "2": "Disagree", 
            "3": "Neutral",
            "4": "Agree",
            "5": "Strongly Agree"
        }
    }
}

BOOLEAN CONFIG:
{
    "options": {
        "trueLabel": "Yes",
        "falseLabel": "No"
    }
}

TEXT CONFIG:
{
    "text": {
        "placeholder": "Enter your response...",
        "charLimit": 1000
    }
}
"""